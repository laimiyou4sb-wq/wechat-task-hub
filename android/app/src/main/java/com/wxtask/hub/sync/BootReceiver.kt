package com.wxtask.hub.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.wxtask.hub.WxTaskApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** 开机 / 改时区 / 应用更新后：重排提醒 + 恢复周期同步 + 立即同步一次。 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext as? WxTaskApp ?: return
        CoroutineScope(Dispatchers.IO).launch {
            runCatching { app.container.alarmScheduler.rescheduleAll() }
            runCatching { app.container.syncScheduler.schedulePeriodic() }
            runCatching { app.container.syncScheduler.requestImmediate("boot", dedupe = true) }
        }
    }
}
