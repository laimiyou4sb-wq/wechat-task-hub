package com.wxtask.hub.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wxtask.hub.AppContainer
import com.wxtask.hub.data.local.SyncStateEntity
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.data.prefs.AppSettings
import com.wxtask.hub.sync.SyncOutcome
import com.wxtask.hub.util.TimeUtil
import java.time.LocalDate
import java.time.LocalDateTime
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

enum class HomeTab(val label: String) {
    TODAY("今天"),
    WEEK("本周"),
    LATER("以后"),
    DONE("已完成"),
}

enum class ArchiveFilter(val label: String) {
    DONE("已完成"),
    IGNORED("已忽略"),
    SNOOZED("稍后"),
}

data class HomeCounts(
    val today: Int = 0,
    val week: Int = 0,
    val later: Int = 0,
    val done: Int = 0,
    val ignored: Int = 0,
    val snoozed: Int = 0,
)

data class TaskSectionUi(val title: String?, val tasks: List<TaskEntity>)

data class HomeUiState(
    val sections: List<TaskSectionUi> = emptyList(),
    val counts: HomeCounts = HomeCounts(),
) {
    val isEmpty: Boolean get() = sections.all { it.tasks.isEmpty() }
}

class HomeViewModel(private val container: AppContainer) : ViewModel() {

    val tab = MutableStateFlow(HomeTab.TODAY)
    val archiveFilter = MutableStateFlow(ArchiveFilter.DONE)
    val refreshing = MutableStateFlow(false)

    private val tasks = container.repository.observeTasks()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    val settings: StateFlow<AppSettings> = container.settingsStore.settings
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), AppSettings())

    val syncState: StateFlow<SyncStateEntity?> = container.db.syncStateDao().observe()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val syncing = container.syncEngine.syncing

    val outboxCount: StateFlow<Int> = container.repository.observeOutboxCount()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val notices = _notices.asSharedFlow()

    val ui: StateFlow<HomeUiState> = combine(tasks, tab, archiveFilter) { list, selectedTab, filter ->
        buildUi(list, selectedTab, filter)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HomeUiState())

    init {
        viewModelScope.launch {
            container.syncEngine.notices.collect { _notices.emit(it) }
        }
    }

    fun selectTab(value: HomeTab) {
        tab.value = value
    }

    fun selectArchive(value: ArchiveFilter) {
        archiveFilter.value = value
    }

    fun refresh() {
        if (refreshing.value) return
        viewModelScope.launch {
            refreshing.value = true
            val outcome = container.syncNow()
            refreshing.value = false
            if (outcome is SyncOutcome.Failed) _notices.tryEmit(outcome.message)
        }
    }

    fun toggleDone(task: TaskEntity) {
        viewModelScope.launch { container.repository.toggleDone(task.id) }
    }

    fun restore(task: TaskEntity) {
        viewModelScope.launch { container.repository.restore(task.id) }
    }

    /** 首次自动请求通知权限后调用，避免每次回首页都重复弹窗。 */
    fun markNotificationPrompted() {
        viewModelScope.launch { container.settingsStore.markNotificationPrompted() }
    }

    private fun buildUi(list: List<TaskEntity>, selectedTab: HomeTab, filter: ArchiveFilter): HomeUiState {
        val pending = list.filter { it.status == "pending" }
        val counts = HomeCounts(
            today = pending.count { TimeUtil.bucketOf(it.status, it.dueAt) == "today" },
            week = pending.count { TimeUtil.bucketOf(it.status, it.dueAt) == "week" },
            later = pending.count { TimeUtil.bucketOf(it.status, it.dueAt) == "later" },
            done = list.count { it.status == "done" },
            ignored = list.count { it.status == "ignored" },
            snoozed = list.count { it.status == "snoozed" },
        )
        val now = TimeUtil.now()
        val sections = when (selectedTab) {
            HomeTab.TODAY -> {
                val todayTasks = pending
                    .filter { TimeUtil.bucketOf(it.status, it.dueAt) == "today" }
                    .sortedWith(dueComparator())
                val overdue = todayTasks.filter { task ->
                    val due = TimeUtil.parseOrNull(task.dueAt)
                    due != null && due.isBefore(now)
                }
                val dueToday = todayTasks.filterNot { task ->
                    val due = TimeUtil.parseOrNull(task.dueAt)
                    due != null && due.isBefore(now)
                }
                listOfNotNull(
                    overdue.takeIf { it.isNotEmpty() }?.let { TaskSectionUi("已逾期", it) },
                    dueToday.takeIf { it.isNotEmpty() }?.let { TaskSectionUi("今天", it) },
                )
            }
            HomeTab.WEEK -> {
                val weekTasks = pending
                    .filter { TimeUtil.bucketOf(it.status, it.dueAt) == "week" }
                    .sortedWith(dueComparator())
                val grouped = LinkedHashMap<LocalDate, MutableList<TaskEntity>>()
                weekTasks.forEach { task ->
                    val date: LocalDate = TimeUtil.parseOrNull(task.dueAt)?.toLocalDate() ?: LocalDate.now()
                    grouped.getOrPut(date) { mutableListOf() }.add(task)
                }
                grouped.map { (date, items) ->
                    TaskSectionUi("${TimeUtil.weekdayLabel(date)} ${date.monthValue} 月 ${date.dayOfMonth} 日", items)
                }
            }
            HomeTab.LATER -> {
                val laterTasks = pending
                    .filter { TimeUtil.bucketOf(it.status, it.dueAt) == "later" }
                    .sortedWith(dueComparator())
                listOfNotNull(laterTasks.takeIf { it.isNotEmpty() }?.let { TaskSectionUi("以后", it) })
            }
            HomeTab.DONE -> {
                val status = when (filter) {
                    ArchiveFilter.DONE -> "done"
                    ArchiveFilter.IGNORED -> "ignored"
                    ArchiveFilter.SNOOZED -> "snoozed"
                }
                val archived = list
                    .filter { it.status == status }
                    .sortedWith(updatedComparator())
                listOf(TaskSectionUi(null, archived))
            }
        }
        return HomeUiState(sections = sections, counts = counts)
    }

    private fun <T : Comparable<T>> nullsLastComparator(): Comparator<T?> = Comparator { a, b ->
        when {
            a == null && b == null -> 0
            a == null -> 1
            b == null -> -1
            else -> a.compareTo(b)
        }
    }

    private fun dueComparator(): Comparator<TaskEntity> =
        compareBy(nullsLastComparator<LocalDateTime>()) { TimeUtil.parseOrNull(it.dueAt) }

    private fun updatedComparator(): Comparator<TaskEntity> =
        compareByDescending(nullsLastComparator<LocalDateTime>()) { TimeUtil.parseOrNull(it.updatedAt) }
}
