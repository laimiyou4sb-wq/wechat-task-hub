package com.wxtask.hub.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.wxtask.hub.WxTaskApp
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/** 后台同步调度：15 分钟周期 + 操作/启动/回前台/连网时立即尝试。 */
class SyncScheduler(private val context: Context) {

    private val lastForegroundRequestAt = AtomicLong(0L)

    fun schedulePeriodic() {
        val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(networkConstraints())
            .build()
        WorkManager.getInstance(context)
            .enqueueUniquePeriodicWork(PERIODIC_WORK, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    /**
     * @param dedupe 用于启动/回前台/连网这类可合并的触发：2 秒内只排一次，避免启动瞬间排队多个 Worker。
     *               本地操作触发的同步不合并，保证操作尽快推送。
     */
    fun requestImmediate(reason: String, dedupe: Boolean = false) {
        if (dedupe) {
            val now = System.currentTimeMillis()
            val last = lastForegroundRequestAt.get()
            if (now - last < 2_000L) return
            lastForegroundRequestAt.set(now)
        }
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(networkConstraints())
            .addTag(TAG_SYNC)
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork(IMMEDIATE_WORK, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }

    private fun networkConstraints(): Constraints =
        Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    companion object {
        private const val PERIODIC_WORK = "wxtask-periodic-sync"
        private const val IMMEDIATE_WORK = "wxtask-immediate-sync"
        private const val TAG_SYNC = "wxtask-sync"
    }
}

class SyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext as? WxTaskApp ?: return Result.success()
        return when (app.container.syncEngine.sync()) {
            is SyncOutcome.Failed -> Result.retry()
            else -> Result.success()
        }
    }
}
