package com.wxtask.hub.sync

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import com.wxtask.hub.data.local.TaskDao
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.util.TimeUtil
import java.time.LocalDateTime

/**
 * 到点提醒：pending 且 due_at 在未来的任务预约 AlarmManager。
 * 有精确闹钟权限用 setExactAndAllowWhileIdle，否则退化为 setAndAllowWhileIdle。
 * 同步、开机、改时区以及完成任务/忽略/稍后/删除后都会重排。
 */
class AlarmScheduler(
    private val context: Context,
    private val taskDao: TaskDao,
) {

    private val alarmManager: AlarmManager? =
        context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager

    fun syncTask(task: TaskEntity) {
        val due = TimeUtil.parseOrNull(task.dueAt)
        if (task.status == "pending" && due != null && due.isAfter(TimeUtil.now())) {
            schedule(task.id, due)
        } else {
            cancel(task.id)
        }
    }

    suspend fun rescheduleAll() {
        taskDao.getAll().forEach { syncTask(it) }
    }

    fun schedule(taskId: String, due: LocalDateTime) {
        val manager = alarmManager ?: return
        val triggerAt = due.atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()
        val pending = pendingIntent(taskId)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !manager.canScheduleExactAlarms()) {
                manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending)
            } else {
                // setAlarmClock 精确且不受 Doze 的 allow-while-idle 限流，国产 ROM 上比
                // setExactAndAllowWhileIdle 更可靠；系统会显示闹钟图标，点按可打开对应任务。
                val showIntent = PendingIntent.getActivity(
                    context,
                    taskId.hashCode(),
                    Intent(context, com.wxtask.hub.MainActivity::class.java).apply {
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                        putExtra(com.wxtask.hub.MainActivity.EXTRA_OPEN_TASK, taskId)
                    },
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                )
                manager.setAlarmClock(AlarmManager.AlarmClockInfo(triggerAt, showIntent), pending)
            }
        } catch (_: SecurityException) {
            runCatching { manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending) }
        } catch (_: Exception) {
            // 个别 ROM 在极端省电模式下会拒绝闹钟；忽略，下次同步会重排
        }
    }

    fun cancel(taskId: String) {
        val manager = alarmManager ?: return
        runCatching { manager.cancel(pendingIntent(taskId)) }
    }

    private fun pendingIntent(taskId: String): PendingIntent {
        val intent = Intent(context, ReminderReceiver::class.java).apply {
            action = "$ACTION_REMIND$taskId"
            putExtra(EXTRA_TASK_ID, taskId)
        }
        return PendingIntent.getBroadcast(
            context,
            taskId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    companion object {
        const val ACTION_REMIND = "com.wxtask.hub.REMIND:"
        const val EXTRA_TASK_ID = "task_id"
    }
}
