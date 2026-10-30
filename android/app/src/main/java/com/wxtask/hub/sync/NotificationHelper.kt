package com.wxtask.hub.sync

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.wxtask.hub.MainActivity
import com.wxtask.hub.R
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.util.TimeUtil

/** 新待办汇总通知 + 到点提醒。 */
class NotificationHelper(private val context: Context) {

    private val manager = NotificationManagerCompat.from(context)

    fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val newTasks = NotificationChannel(
            CHANNEL_NEW_TASKS,
            context.getString(R.string.notification_channel_tasks),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = context.getString(R.string.notification_channel_tasks_desc)
        }
        val due = NotificationChannel(
            CHANNEL_DUE,
            context.getString(R.string.notification_channel_reminder),
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = context.getString(R.string.notification_channel_reminder_desc)
            enableVibration(true)
        }
        manager.createNotificationChannels(listOf(newTasks, due))
    }

    fun notificationsEnabled(): Boolean = manager.areNotificationsEnabled()

    fun showNewTasks(tasks: List<TaskEntity>) {
        if (tasks.isEmpty() || !notificationsEnabled()) return
        val count = tasks.size
        val text = tasks.take(3).joinToString("\n") { "· ${it.title}" } +
            if (count > 3) "\n…等 $count 条" else ""
        val notification = NotificationCompat.Builder(context, CHANNEL_NEW_TASKS)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("新增 $count 条待办")
            .setContentText(tasks.first().title)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openAppIntent(null))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        notify(NEW_TASKS_ID, notification)
    }

    fun showDueReminder(task: TaskEntity) {
        if (!notificationsEnabled()) return
        val due = TimeUtil.dueInfo(task.dueAt, task.status)?.text
        val subtitle = listOfNotNull(due, task.groupName?.takeIf { it.isNotBlank() })
            .joinToString(" · ")
        val notification = NotificationCompat.Builder(context, CHANNEL_DUE)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("到点：${task.title}")
            .setContentText(subtitle)
            .setStyle(NotificationCompat.BigTextStyle().bigText(subtitle.ifBlank { task.title }))
            .setAutoCancel(true)
            .setContentIntent(openAppIntent(task.id))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .build()
        notify(task.id.hashCode(), notification)
    }

    private fun openAppIntent(taskId: String?): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            if (taskId != null) putExtra(MainActivity.EXTRA_OPEN_TASK, taskId)
        }
        return PendingIntent.getActivity(
            context,
            taskId?.hashCode() ?: 0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun notify(id: Int, notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        try {
            manager.notify(id, notification)
        } catch (_: SecurityException) {
            // 用户刚撤销通知权限，忽略即可
        }
    }

    companion object {
        const val CHANNEL_NEW_TASKS = "new_tasks"
        const val CHANNEL_DUE = "due_reminder"
        private const val NEW_TASKS_ID = 1001
    }
}
