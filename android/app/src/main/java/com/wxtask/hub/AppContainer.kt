package com.wxtask.hub

import android.content.Context
import android.util.Base64
import com.wxtask.hub.data.TaskRepository
import com.wxtask.hub.data.TaskStore
import com.wxtask.hub.data.local.AppDatabase
import com.wxtask.hub.data.prefs.Endpoint
import com.wxtask.hub.data.prefs.SettingsStore
import com.wxtask.hub.data.remote.ApiResult
import com.wxtask.hub.data.remote.PairingPayload
import com.wxtask.hub.data.remote.PingDto
import com.wxtask.hub.data.remote.TaskApi
import com.wxtask.hub.sync.AlarmScheduler
import com.wxtask.hub.sync.NotificationHelper
import com.wxtask.hub.sync.SyncEngine
import com.wxtask.hub.sync.SyncOutcome
import com.wxtask.hub.sync.SyncScheduler
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json

sealed interface PairResult {
    data class Success(val message: String) : PairResult
    data class Error(val message: String) : PairResult
}

/** 手工依赖容器（不引入 DI 框架，保持构建简单）。 */
class AppContainer(context: Context) {

    private val appContext = context.applicationContext
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    val db: AppDatabase = AppDatabase.build(appContext)
    val settingsStore = SettingsStore(appContext)
    val notifier = NotificationHelper(appContext)
    val alarmScheduler = AlarmScheduler(appContext, db.taskDao())
    val syncScheduler = SyncScheduler(appContext)
    val api = TaskApi(db.syncStateDao())
    val taskStore = TaskStore(db)
    val repository = TaskRepository(db, alarmScheduler, syncScheduler)
    val syncEngine = SyncEngine(db, settingsStore, api, taskStore, notifier, alarmScheduler)

    /** 通知点击 / 深链要打开的任务 id。 */
    val pendingOpenTask = MutableStateFlow<String?>(null)

    /** 连续收到多个配对深链时串行处理，避免并发清库/同步交叉。 */
    private val pairingMutex = Mutex()

    fun openTask(taskId: String) {
        pendingOpenTask.value = taskId
    }

    fun consumeOpenTask() {
        pendingOpenTask.value = null
    }

    fun schedulePeriodicSync() = syncScheduler.schedulePeriodic()

    fun requestImmediateSync(reason: String, dedupe: Boolean = false) =
        syncScheduler.requestImmediate(reason, dedupe)

    suspend fun syncNow(): SyncOutcome = syncEngine.sync()

    suspend fun pingServer(): ApiResult<PingDto> = api.ping(settingsStore.current())

    suspend fun testEndpoint(url: String, token: String): ApiResult<PingDto> = api.pingAt(url, token)

    /** 粘贴配对码：保存 endpoints/token → 清空本地缓存 → 触发全量同步。 */
    suspend fun pairWithCode(rawCode: String): PairResult = pairingMutex.withLock {
        val payload = decodePairingCode(rawCode)
            ?: return@withLock PairResult.Error("配对码无法解析，请重新复制（只需 base64url 部分）")
        if (payload.token.isBlank()) return@withLock PairResult.Error("配对码里没有 token")
        val endpoints = normalizeEndpoints(payload.endpoints)
        if (endpoints.isEmpty()) return@withLock PairResult.Error("配对码里没有可用的服务器地址")
        db.syncStateDao().ensureRow()
        settingsStore.savePairing(
            deviceName = payload.name.ifBlank { "电脑端" },
            token = payload.token.trim(),
            endpoints = endpoints,
        )
        when (val outcome = syncEngine.resetAndSync { taskStore.clearAll() }) {
            is SyncOutcome.Success -> PairResult.Success("配对成功：${payload.name.ifBlank { "电脑端" }}")
            is SyncOutcome.Failed -> PairResult.Error("配对信息已保存，但同步失败：${outcome.message}")
            SyncOutcome.Unauthorized -> PairResult.Error("配对信息已保存，但令牌无效（401），请检查 token")
            SyncOutcome.NotPaired -> PairResult.Error("配对信息保存失败")
        }
    }

    /** 手动配对：地址 + token。 */
    suspend fun pairManual(rawUrl: String, rawToken: String): PairResult = pairingMutex.withLock {
        val token = rawToken.trim()
        if (token.isEmpty()) return@withLock PairResult.Error("请填写 token")
        var url = rawUrl.trim()
        if (url.isEmpty()) return@withLock PairResult.Error("请填写电脑端地址")
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        url = url.trimEnd('/')
        db.syncStateDao().ensureRow()
        settingsStore.saveManual(url, token)
        when (val outcome = syncEngine.resetAndSync { taskStore.clearAll() }) {
            is SyncOutcome.Success -> PairResult.Success("配对成功，已同步 ${outcome.newTaskCount} 条新待办")
            is SyncOutcome.Failed -> PairResult.Error("已保存地址，但同步失败：${outcome.message}")
            SyncOutcome.Unauthorized -> PairResult.Error("token 无效（401），请检查后重试")
            SyncOutcome.NotPaired -> PairResult.Error("配对信息保存失败")
        }
    }

    fun decodePairingCode(rawCode: String): PairingPayload? {
        val cleaned = rawCode
            .trim()
            .replace("\n", "")
            .replace("\r", "")
            .replace(" ", "")
            .trimEnd('=')
        if (cleaned.isEmpty()) return null
        val bytes = runCatching {
            Base64.decode(cleaned, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
        }.recoverCatching {
            Base64.decode(cleaned, Base64.DEFAULT)
        }.getOrNull() ?: return null
        return runCatching { json.decodeFromString<PairingPayload>(String(bytes, Charsets.UTF_8)) }.getOrNull()
    }

    private fun normalizeEndpoints(endpoints: List<com.wxtask.hub.data.remote.EndpointDto>): List<Endpoint> =
        endpoints
            .filter { it.url.isNotBlank() }
            .map { Endpoint(kind = it.kind, url = it.url.trim().trimEnd('/'), priority = it.priority) }
            .distinctBy { it.url }
            .sortedWith(compareBy({ if (it.kind == "lan") 0 else 1 }, { it.priority }))

    fun syncOutcomeMessage(outcome: SyncOutcome): String = when (outcome) {
        is SyncOutcome.Success ->
            if (outcome.newTaskCount > 0) "同步完成，新增 ${outcome.newTaskCount} 条待办" else "同步完成"
        is SyncOutcome.Failed -> outcome.message
        SyncOutcome.Unauthorized -> "配对令牌失效，请重新配对"
        SyncOutcome.NotPaired -> "尚未配对"
    }

    fun appVersionName(): String = runCatching {
        appContext.packageManager.getPackageInfo(appContext.packageName, 0).versionName
    }.getOrNull() ?: "1.0.0"
}
