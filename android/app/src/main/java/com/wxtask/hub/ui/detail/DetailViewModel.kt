package com.wxtask.hub.ui.detail

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wxtask.hub.AppContainer
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.data.remote.ApiResult
import com.wxtask.hub.data.remote.EventDto
import com.wxtask.hub.data.remote.SourceDto
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class DetailViewModel(
    private val container: AppContainer,
    private val taskId: String,
) : ViewModel() {

    val task: StateFlow<TaskEntity?> = container.repository.observeTask(taskId)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val editTitle = MutableStateFlow("")
    val editNotes = MutableStateFlow("")
    val editDueAt = MutableStateFlow<String?>(null)
    val editPriority = MutableStateFlow("normal")

    val detailLoading = MutableStateFlow(true)
    val sources = MutableStateFlow<List<SourceDto>>(emptyList())
    val event = MutableStateFlow<EventDto?>(null)
    val detailError = MutableStateFlow<String?>(null)

    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val notices = _notices.asSharedFlow()

    val dirty: StateFlow<Boolean> = combine(
        task,
        editTitle,
        editNotes,
        editDueAt,
        editPriority,
    ) { current, title, notes, dueAt, priority ->
        current != null && (
            current.title != title ||
                current.notes != notes ||
                current.dueAt != dueAt ||
                current.priority != priority
            )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), false)

    init {
        viewModelScope.launch {
            task.collect { current ->
                if (current != null && !dirty.value) applyTaskToEdit(current)
            }
        }
        loadDetail()
    }

    fun setTitle(value: String) {
        editTitle.value = value
    }

    fun setNotes(value: String) {
        editNotes.value = value
    }

    fun setDueAt(value: String?) {
        editDueAt.value = value
    }

    fun setPriority(value: String) {
        editPriority.value = value
    }

    fun save() {
        val current = task.value ?: return
        val title = editTitle.value.trim()
        if (title.isEmpty()) {
            _notices.tryEmit("标题不能为空")
            return
        }
        if (!dirty.value) {
            _notices.tryEmit("没有需要保存的修改")
            return
        }
        viewModelScope.launch {
            container.repository.updateFields(
                id = current.id,
                title = title,
                notes = editNotes.value,
                dueAt = editDueAt.value,
                priority = editPriority.value,
            )
            _notices.tryEmit("已保存，正在同步到电脑端")
        }
    }

    fun resetChanges() {
        task.value?.let { applyTaskToEdit(it) }
    }

    fun toggleDone() {
        val current = task.value ?: return
        viewModelScope.launch { container.repository.toggleDone(current.id) }
    }

    fun snoozeOneHour() {
        val current = task.value ?: return
        viewModelScope.launch { container.repository.snoozeOneHour(current.id) }
    }

    fun ignore() {
        val current = task.value ?: return
        viewModelScope.launch { container.repository.ignore(current.id) }
    }

    fun restore() {
        val current = task.value ?: return
        viewModelScope.launch { container.repository.restore(current.id) }
    }

    fun delete() {
        viewModelScope.launch {
            container.repository.delete(taskId)
            _notices.tryEmit("已删除")
        }
    }

    fun loadDetail() {
        if (taskId.startsWith("local-")) {
            detailLoading.value = false
            return
        }
        viewModelScope.launch {
            val settings = container.settingsStore.current()
            if (!settings.paired) {
                detailLoading.value = false
                detailError.value = "尚未配对，无法加载微信原文"
                return@launch
            }
            detailLoading.value = true
            detailError.value = null
            var attempt = 0
            while (attempt < 2) {
                attempt++
                when (val result = container.api.taskDetail(settings, taskId)) {
                    is ApiResult.Success -> {
                        sources.value = result.data.sources
                        event.value = result.data.event
                        result.data.task?.let { container.taskStore.applyServerTask(it) }
                        container.taskStore.cacheEvent(taskId, result.data.event)
                        detailLoading.value = false
                        detailError.value = null
                        if (!dirty.value) task.value?.let { applyTaskToEdit(it) }
                        return@launch
                    }
                    is ApiResult.Unauthorized -> {
                        detailLoading.value = false
                        detailError.value = "令牌失效，请重新配对"
                        return@launch
                    }
                    is ApiResult.NotFound -> {
                        detailLoading.value = false
                        detailError.value = "任务不存在或已从电脑端删除"
                        return@launch
                    }
                    is ApiResult.Failure -> {
                        if (attempt < 2) {
                            kotlinx.coroutines.delay(900)
                            continue
                        }
                        detailLoading.value = false
                        detailError.value = result.message
                    }
                }
            }
        }
    }

    private fun applyTaskToEdit(current: TaskEntity) {
        editTitle.value = current.title
        editNotes.value = current.notes
        editDueAt.value = current.dueAt
        editPriority.value = current.priority
    }
}
