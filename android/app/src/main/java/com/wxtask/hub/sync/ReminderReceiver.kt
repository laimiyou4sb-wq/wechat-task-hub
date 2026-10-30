package com.wxtask.hub.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.wxtask.hub.WxTaskApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** 到点提醒广播：查库确认任务仍是待办后发通知。 */
class ReminderReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val taskId = intent.getStringExtra(AlarmScheduler.EXTRA_TASK_ID) ?: return
        val app = context.applicationContext as? WxTaskApp ?: return
        val pendingResult = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val settings = app.container.settingsStore.current()
                if (!settings.notifyDue) return@launch
                val task = app.container.db.taskDao().get(taskId) ?: return@launch
                if (task.status == "pending") {
                    app.container.notifier.showDueReminder(task)
                }
            } finally {
                pendingResult.finish()
            }
        }
    }
}
