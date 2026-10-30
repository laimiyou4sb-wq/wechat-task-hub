package com.wxtask.hub.util

import java.time.DayOfWeek
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** 任务 DTO 使用无时区本地时间：yyyy-MM-ddTHH:mm:ss（Asia/Shanghai）。 */
object TimeUtil {

    private val ISO: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")
    private val TIME: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")
    private val MONTH_DAY: DateTimeFormatter = DateTimeFormatter.ofPattern("M月d日")

    fun now(): LocalDateTime = LocalDateTime.now().withNano(0)

    fun nowIso(): String = now().format(ISO)

    fun format(value: LocalDateTime): String = value.withNano(0).format(ISO)

    /** 宽松解析：接受 `yyyy-MM-ddTHH:mm:ss`、`yyyy-MM-ddTHH:mm`，忽略毫秒/时区后缀。 */
    fun parseOrNull(raw: String?): LocalDateTime? {
        val value = raw?.trim().orEmpty()
        if (value.isEmpty()) return null
        val core = value
            .substringBefore('Z')
            .substringBefore('+')
            .substringBefore('.')
            .trim()
        return runCatching { LocalDateTime.parse(core, ISO) }
            .recoverCatching { LocalDateTime.parse(core, DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm")) }
            .recoverCatching { LocalDateTime.parse(core) }
            .getOrNull()
    }

    fun isPast(dueAt: String?): Boolean {
        val due = parseOrNull(dueAt) ?: return false
        return due.isBefore(now())
    }

    fun weekdayLabel(date: LocalDate): String = when (date.dayOfWeek) {
        DayOfWeek.MONDAY -> "周一"
        DayOfWeek.TUESDAY -> "周二"
        DayOfWeek.WEDNESDAY -> "周三"
        DayOfWeek.THURSDAY -> "周四"
        DayOfWeek.FRIDAY -> "周五"
        DayOfWeek.SATURDAY -> "周六"
        DayOfWeek.SUNDAY -> "周日"
    }

    fun dayLabel(date: LocalDate): String {
        val today = LocalDate.now()
        return when (ChronoUnit.DAYS.between(today, date)) {
            0L -> "今天"
            1L -> "明天"
            2L -> "后天"
            else -> {
                val year = if (date.year != today.year) "${date.year}年" else ""
                "$year${date.format(MONTH_DAY)} ${weekdayLabel(date)}"
            }
        }
    }

    fun timeLabel(value: LocalDateTime): String = value.format(TIME)

    /** 列表里的到期标签。 */
    fun dueInfo(dueAt: String?, status: String): DueInfo? {
        val due = parseOrNull(dueAt) ?: return null
        val time = timeLabel(due)
        if (status == "done" || status == "ignored") {
            return DueInfo("${dayLabel(due.toLocalDate())} $time", overdue = false)
        }
        val now = now()
        if (due.isBefore(now)) {
            val day = if (due.toLocalDate() == now.toLocalDate()) "今天" else dayLabel(due.toLocalDate())
            return DueInfo("已逾期 · $day $time", overdue = true)
        }
        return DueInfo("${dayLabel(due.toLocalDate())} $time", overdue = false)
    }

    /** 契约 §7：逾期和今天 → today；7 天内 → week；更远或无截止 → later。 */
    fun bucketOf(status: String, dueAt: String?): String? {
        if (status != "pending") return null
        val due = parseOrNull(dueAt) ?: return "later"
        val diff = ChronoUnit.DAYS.between(LocalDate.now(), due.toLocalDate())
        return when {
            diff <= 0L -> "today"
            diff <= 7L -> "week"
            else -> "later"
        }
    }

    fun startOfDay(date: LocalDate): LocalDateTime = date.atStartOfDay()

    /** 日期选择器使用 UTC 零点毫秒。 */
    fun localDateToUtcMillis(date: LocalDate): Long =
        date.atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()

    fun utcMillisToLocalDate(millis: Long): LocalDate =
        Instant.ofEpochMilli(millis).atZone(ZoneOffset.UTC).toLocalDate()

    /** 来源消息时间：兼容秒 / 毫秒时间戳。 */
    fun formatEpoch(ts: Long?): String? {
        if (ts == null || ts <= 0L) return null
        val millis = if (ts > 1_000_000_000_000L) ts else ts * 1000L
        val at = Instant.ofEpochMilli(millis).atZone(java.time.ZoneId.systemDefault()).toLocalDateTime()
        return "${dayLabel(at.toLocalDate())} ${timeLabel(at)}"
    }

    /** 「12:30」「昨天 12:30」这类同步时间展示。 */
    fun relativeSyncTime(raw: String?): String {
        val at = parseOrNull(raw) ?: return "从未同步"
        val now = now()
        val between = Duration.between(at, now)
        if (between.isNegative) return at.format(TIME)
        val minutes = between.toMinutes()
        return when {
            minutes < 1 -> "刚刚"
            minutes < 60 -> "${minutes} 分钟前"
            at.toLocalDate() == now.toLocalDate() -> at.format(TIME)
            at.toLocalDate() == now.toLocalDate().minusDays(1) -> "昨天 ${at.format(TIME)}"
            else -> "${dayLabel(at.toLocalDate())} ${at.format(TIME)}"
        }
    }

    fun snoozeUntilOneHour(): String = now().plusHours(1).format(ISO)
}

data class DueInfo(val text: String, val overdue: Boolean)
