package com.wxtask.hub.ui.newtask

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.wxtask.hub.AppContainer
import com.wxtask.hub.ui.components.DueEditor
import com.wxtask.hub.ui.components.PriorityChips
import com.wxtask.hub.ui.components.ScreenTopBar
import com.wxtask.hub.util.TimeUtil
import java.time.LocalDate
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch

class NewTaskViewModel(private val container: AppContainer) : ViewModel() {

    val title = MutableStateFlow("")
    val notes = MutableStateFlow("")
    val dueAt = MutableStateFlow<String?>(null)
    val priority = MutableStateFlow("normal")
    val submitting = MutableStateFlow(false)

    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val notices = _notices.asSharedFlow()

    private val _created = MutableSharedFlow<String>(extraBufferCapacity = 1)
    val created = _created.asSharedFlow()

    fun create() {
        val value = title.value.trim()
        if (value.isEmpty()) {
            _notices.tryEmit("请先填写任务标题")
            return
        }
        if (submitting.value) return
        viewModelScope.launch {
            submitting.value = true
            val id = container.repository.create(
                title = value,
                notes = notes.value.trim(),
                dueAt = dueAt.value,
                priority = priority.value,
            )
            submitting.value = false
            _created.tryEmit(id)
        }
    }
}

@Composable
fun NewTaskScreen(
    container: AppContainer,
    onBack: () -> Unit,
    onCreated: (String) -> Unit,
) {
    val vm: NewTaskViewModel = viewModel(
        factory = viewModelFactory { initializer { NewTaskViewModel(container) } },
    )
    val title by vm.title.collectAsStateWithLifecycle()
    val notes by vm.notes.collectAsStateWithLifecycle()
    val dueAt by vm.dueAt.collectAsStateWithLifecycle()
    val priority by vm.priority.collectAsStateWithLifecycle()
    val submitting by vm.submitting.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    val keyboard = LocalSoftwareKeyboardController.current

    LaunchedEffect(Unit) {
        vm.notices.collect { snackbar.showSnackbar(it) }
    }
    LaunchedEffect(Unit) {
        vm.created.collect { id ->
            keyboard?.hide()
            onCreated(id)
        }
    }

    Box(Modifier.fillMaxSize()) {
        Column(
            Modifier
                .fillMaxSize()
                .statusBarsPadding()
                .imePadding(),
        ) {
            ScreenTopBar(title = "新建任务", onBack = onBack)
            Column(
                modifier = Modifier
                    .weight(1f)
                    .verticalScroll(rememberScrollState())
                    .navigationBarsPadding()
                    .padding(horizontal = 16.dp, vertical = 4.dp),
            ) {
                OutlinedTextField(
                    value = title,
                    onValueChange = { vm.title.value = it },
                    label = { Text("标题") },
                    placeholder = { Text("例如：周五前提交报名表") },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 1,
                    maxLines = 3,
                )
                Spacer(Modifier.height(14.dp))
                Text(
                    text = "优先级",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(6.dp))
                PriorityChips(selected = priority, onSelect = { vm.priority.value = it })
                Spacer(Modifier.height(14.dp))
                Text(
                    text = "截止时间",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(6.dp))
                QuickDueChips(
                    dueAt = dueAt,
                    onPick = { vm.dueAt.value = it },
                )
                Spacer(Modifier.height(8.dp))
                DueEditor(dueAt = dueAt, onDueChange = { vm.dueAt.value = it })
                Spacer(Modifier.height(14.dp))
                OutlinedTextField(
                    value = notes,
                    onValueChange = { vm.notes.value = it },
                    label = { Text("备注（可选）") },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 3,
                    maxLines = 8,
                )
                Spacer(Modifier.height(20.dp))
                Button(
                    onClick = vm::create,
                    enabled = !submitting && title.isNotBlank(),
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 50.dp),
                ) {
                    if (submitting) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(18.dp),
                            strokeWidth = 2.dp,
                            color = MaterialTheme.colorScheme.onPrimary,
                        )
                        Spacer(Modifier.width(8.dp))
                    }
                    Text("添加任务", fontWeight = FontWeight.SemiBold)
                }
                Spacer(Modifier.height(28.dp))
            }
        }
        SnackbarHost(
            hostState = snackbar,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(horizontal = 16.dp, vertical = 16.dp),
        )
    }
}

@Composable
private fun QuickDueChips(dueAt: String?, onPick: (String?) -> Unit) {
    val colors = MaterialTheme.colorScheme
    val today = LocalDate.now()
    val fridayDelta = ((5 - today.dayOfWeek.value) + 7) % 7
    val chips = listOf(
        "今天 18:00" to TimeUtil.format(today.atTime(18, 0)),
        "明天 09:00" to TimeUtil.format(today.plusDays(1).atTime(9, 0)),
        "本周五 18:00" to TimeUtil.format(today.plusDays(fridayDelta.toLong()).atTime(18, 0)),
        "清除" to null,
    )
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        chips.forEach { (label, value) ->
            val isSelected = value != null && value == dueAt
            Surface(
                shape = RoundedCornerShape(50),
                color = if (isSelected) colors.primary else colors.surfaceVariant,
                modifier = Modifier
                    .heightIn(min = 40.dp)
                    .clip(RoundedCornerShape(50))
                    .clickable { onPick(value) },
            ) {
                Box(
                    Modifier.padding(horizontal = 10.dp, vertical = 8.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = label,
                        fontSize = 11.sp,
                        maxLines = 1,
                        color = if (isSelected) colors.onPrimary else colors.onSurfaceVariant,
                    )
                }
            }
        }
    }
}
