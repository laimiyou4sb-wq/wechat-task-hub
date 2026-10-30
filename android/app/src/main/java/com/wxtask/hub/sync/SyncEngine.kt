package com.wxtask.hub.sync

import com.wxtask.hub.data.TaskStore
import com.wxtask.hub.data.local.AppDatabase
import com.wxtask.hub.data.local.OutboxEntity
import com.wxtask.hub.data.local.SyncStateEntity
import com.wxtask.hub.data.prefs.AppSettings
import com.wxtask.hub.data.prefs.SettingsStore
import com.wxtask.hub.data.remote.ActionDto
import com.wxtask.hub.data.remote.ApiResult
import com.wxtask.hub.data.remote.TaskApi
import com.wxtask.hub.data.remote.TaskDto
import com.wxtask.hub.util.TimeUtil
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

sealed interface SyncOutcome {
    data class Success(val newTaskCount: Int = 0) : SyncOutcome
    data class Failed(val message: String) : SyncOutcome
    data object Unauthorized : SyncOutcome
    data object NotPaired : SyncOutcome
}

/**
 * 同步引擎（契约 §6）：先推 outbox（幂等、create 映射 local_id），再按 rev 游标拉增量。
 * 整个同步过程串行（Mutex），Worker / 手动刷新 / 操作后推送共用同一个引擎。
 */
class SyncEngine(
    private val db: AppDatabase,
    private val settingsStore: SettingsStore,
    private val api: TaskApi,
    private val taskStore: TaskStore,
    private val notifier: NotificationHelper,
    private val alarmScheduler: AlarmScheduler,
) {

    private val mutex = Mutex()
    private val json = Json { ignoreUnknownKeys = true }

    private val _syncing = MutableStateFlow(false)
    val syncing = _syncing.asStateFlow()

    private val _notices = MutableSharedFlow<String>(
        extraBufferCapacity = 8,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    val notices = _notices.asSharedFlow()

    suspend fun sync(): SyncOutcome = mutex.withLock {
        _syncing.value = true
        try {
            runSyncWithRetry()
        } finally {
            _syncing.value = false
        }
    }

    /**
     * 重新配对：先清空本地缓存，再在同一个同步锁内做全量同步。
     * 避免「清库」和另一个并发同步交叉，导致本地只剩半页任务。
     */
    suspend fun resetAndSync(clearLocal: suspend () -> Unit): SyncOutcome = mutex.withLock {
        _syncing.value = true
        try {
            clearLocal()
            runSyncWithRetry()
        } finally {
            _syncing.value = false
        }
    }

    private suspend fun runSyncWithRetry(): SyncOutcome {
        var outcome = doSync()
        if (outcome is SyncOutcome.Failed) {
            // 局域网/USB 共享网络偶尔会瞬断（复用连接被重置等），稍等后整体重试一次
            kotlinx.coroutines.delay(900)
            outcome = doSync()
        }
        return outcome
    }

    private suspend fun doSync(): SyncOutcome {
        val settings = settingsStore.current()
        if (!settings.paired) return SyncOutcome.NotPaired
        db.syncStateDao().ensureRow()

        // 自愈：本地任务被清空但 cursor 仍大于 0（例如配对/服务端重启交叉），强制拉一次 full 快照
        val initial = db.syncStateDao().get() ?: SyncStateEntity()
        if (initial.cursor > 0L && db.taskDao().allIds().isEmpty() && db.outboxDao().count() == 0) {
            db.syncStateDao().save(initial.copy(cursor = 0L))
        }

        // 1) 推 outbox（失败即本次同步结束，保留队列）
        when (val push = pushOutbox(settings)) {
            is PushOutcome.Network -> {
                markError(push.message)
                return SyncOutcome.Failed(push.message)
            }
            PushOutcome.Unauthorized -> {
                settingsStore.setTokenInvalid(true)
                return SyncOutcome.Unauthorized
            }
            PushOutcome.Done -> Unit
        }

        // 2) 拉增量
        val state = db.syncStateDao().get() ?: SyncStateEntity()
        val firstSync = state.cursor <= 0L
        val beforeIds: Set<String> = if (firstSync) emptySet() else db.taskDao().allIds().toSet()
        var cursor = state.cursor
        var hasMore = true
        var anyFull = false
        var lastServerTime: String? = null
        val removedIds = mutableListOf<String>()

        while (hasMore) {
            when (val result = api.sync(settings, cursor)) {
                is ApiResult.Unauthorized -> {
                    settingsStore.setTokenInvalid(true)
                    return SyncOutcome.Unauthorized
                }
                is ApiResult.NotFound -> {
                    markError("同步接口不存在，请升级/重启电脑端服务")
                    return SyncOutcome.Failed("同步接口不存在，请升级/重启电脑端服务")
                }
                is ApiResult.Failure -> {
                    markError(result.message)
                    return SyncOutcome.Failed(result.message)
                }
                is ApiResult.Success -> {
                    val data = result.data
                    if (data.full) {
                        anyFull = true
                        val idsBefore = db.taskDao().allIds().toSet()
                        val idsAfter = data.tasks.map { it.id }.toSet()
                        removedIds += idsBefore - idsAfter
                    }
                    removedIds += data.deleted
                    taskStore.applySync(data.tasks, data.deleted, data.full)
                    cursor = data.cursor
                    lastServerTime = data.serverTime ?: TimeUtil.nowIso()
                    val current = db.syncStateDao().get() ?: SyncStateEntity()
                    db.syncStateDao().save(
                        current.copy(cursor = cursor, lastSyncAt = lastServerTime, lastError = null),
                    )
                    hasMore = data.hasMore
                }
            }
        }

        settingsStore.setTokenInvalid(false)

        // 3) 新出现的 pending 任务 → 汇总通知（首次/full 快照不通知）
        val after = db.taskDao().getAll()
        val newPending: List<com.wxtask.hub.data.local.TaskEntity> =
            if (firstSync || anyFull) {
                emptyList()
            } else {
                after.filter { it.status == "pending" && it.id !in beforeIds && !it.isLocalOnly }
            }
        if (newPending.isNotEmpty() && settings.notifyNew) {
            notifier.showNewTasks(newPending)
        }

        // 4) 清理已删除任务的提醒，并重排剩余提醒
        if (removedIds.isNotEmpty()) {
            removedIds.distinct().forEach { alarmScheduler.cancel(it) }
        }
        alarmScheduler.rescheduleAll()

        return SyncOutcome.Success(newPending.size)
    }

    private sealed interface PushOutcome {
        data object Done : PushOutcome
        data object Unauthorized : PushOutcome
        data class Network(val message: String) : PushOutcome
    }

    private suspend fun pushOutbox(settings: AppSettings): PushOutcome {
        var guard = 0
        while (guard++ < 200) {
            val rows = db.outboxDao().firstBatch(BATCH_SIZE)
            if (rows.isEmpty()) return PushOutcome.Done
            when (val response = api.postActions(settings, rows.map { it.toAction() })) {
                is ApiResult.Unauthorized -> return PushOutcome.Unauthorized
                is ApiResult.NotFound -> return PushOutcome.Network("操作接口不存在，请升级/重启电脑端服务")
                is ApiResult.Failure -> return PushOutcome.Network(response.message)
                is ApiResult.Success -> {
                    val results = response.data.results.associateBy { it.actionId }
                    for (row in rows) {
                        val result = results[row.actionId] ?: continue
                        taskStore.applyActionResult(row, result)
                        // 跨批自我冲突重基：本客户端刚推送成功的操作已抬高服务端 rev，
                        // 把该任务剩余待推送行的 client_rev 对齐到最新 rev；否则同一任务的 update
                        // 被 50 条批边界拆到后续批次时，会因旧 client_rev 被服务端误判 stale 丢弃。
                        // create 的 local_id → 真实 id 映射与 rev 改写已由 applyActionResult 完成，这里只补剩余行。
                        // stale=true 表示电脑端确实改过（真实冲突），保持「电脑端优先」，不重基。
                        val latest = result.task
                        if (result.ok && !result.stale && latest != null) {
                            db.outboxDao().rebaseClientRev(latest.id, latest.rev)
                        }
                        if (result.ok && result.stale && row.type == "update") {
                            _notices.tryEmit("电脑端已修改该任务，已更新为最新内容")
                        }
                    }
                    val returned = results.keys
                    val missing = rows.filter { it.actionId !in returned }
                    if (missing.isNotEmpty()) {
                        missing.forEach { db.outboxDao().delete(it.actionId) }
                        _notices.tryEmit("有 ${missing.size} 条操作未得到电脑端确认，已跳过")
                    }
                    // 注意：actions 响应的 cursor 是服务端全局最新 rev，只能说明「推送已到达哪儿」，
                    // 不能当作本地拉取游标。若直接采用，会跳过「离线期间服务端已产生、本地尚未拉取」
                    // 的 rev 区间（漏任务/漏删除墓碑）。本地 cursor 只允许由 /api/app/sync 响应推进。
                }
            }
        }
        return PushOutcome.Done
    }

    private suspend fun markError(message: String) {
        db.syncStateDao().ensureRow()
        db.syncStateDao().markError(message)
    }

    private fun OutboxEntity.toAction(): ActionDto = ActionDto(
        actionId = actionId,
        type = type,
        taskId = taskId,
        localId = localId,
        clientRev = clientRev,
        snoozeUntil = snoozeUntil,
        fields = fieldsJson?.let { raw ->
            runCatching { json.parseToJsonElement(raw) as? JsonObject }.getOrNull()
        },
    )

    companion object {
        private const val BATCH_SIZE = 50

        /** 构造 update/create 的 fields JSON（due_at 用显式 null 表示清空）。 */
        fun actionFields(title: String, notes: String, dueAt: String?, priority: String): String =
            buildJsonObject {
                put("title", title)
                put("notes", notes)
                put("due_at", dueAt?.let { JsonPrimitive(it) } ?: kotlinx.serialization.json.JsonNull)
                put("priority", priority)
            }.toString()
    }
}
