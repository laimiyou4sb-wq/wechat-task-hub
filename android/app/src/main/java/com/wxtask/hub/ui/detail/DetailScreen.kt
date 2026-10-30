package com.wxtask.hub.ui.detail

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.wxtask.hub.AppContainer
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.data.remote.EventDto
import com.wxtask.hub.data.remote.SourceDto
import com.wxtask.hub.ui.components.DueEditor
import com.wxtask.hub.ui.components.PRIORITY_LABELS
import com.wxtask.hub.ui.components.PriorityChips
import com.wxtask.hub.ui.components.ScreenTopBar
import com.wxtask.hub.util.TimeUtil
import kotlinx.coroutines.launch

private val STATUS_LABELS = mapOf(
    "pending" to "待办",
    "done" to "已完成",
    "ignored" to "已忽略",
    "snoozed" to "稍后",
)

@Composable
fun DetailScreen(
    container: AppContainer,
    taskId: String,
    onBack: () -> Unit,
) {
    val vm: DetailViewModel = viewModel(
        key = "detail-$taskId",
        factory = viewModelFactory { initializer { DetailViewModel(container, taskId) } },
    )
    val task by vm.task.collectAsStateWithLifecycle()
    val title by vm.editTitle.collectAsStateWithLifecycle()
    val notes by vm.editNotes.collectAsStateWithLifecycle()
    val dueAt by vm.editDueAt.collectAsStateWithLifecycle()
    val priority by vm.editPriority.collectAsStateWithLifecycle()
    val dirty by vm.dirty.collectAsStateWithLifecycle()
    val loadingDetail by vm.detailLoading.collectAsStateWithLifecycle()
    val sources by vm.sources.collectAsStateWithLifecycle()
    val event by vm.event.collectAsStateWithLifecycle()
    val detailError by vm.detailError.collectAsStateWithLifecycle()

    val snackbar = remember { SnackbarHostState() }
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    var confirmDelete by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        vm.notices.collect { snackbar.showSnackbar(it) }
    }

    Box(Modifier.fillMaxSize()) {
        val current = task
        if (current == null) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
            }
        } else {
            Column(
                Modifier
                    .fillMaxSize()
                    .statusBarsPadding()
                    .imePadding(),
            ) {
                ScreenTopBar(
                    title = "任务详情",
                    onBack = onBack,
                    trailing = {
                        TextButton(
                            onClick = vm::save,
                            enabled = dirty && title.isNotBlank(),
                        ) {
                            Text(
                                text = "保存",
                                color = if (dirty && title.isNotBlank()) {
                                    MaterialTheme.colorScheme.primary
                                } else {
                                    MaterialTheme.colorScheme.outline
                                },
                            )
                        }
                    },
                )
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .verticalScroll(rememberScrollState())
                        .navigationBarsPadding()
                        .padding(horizontal = 16.dp, vertical = 4.dp),
                ) {
                    OutlinedTextField(
                        value = title,
                        onValueChange = vm::setTitle,
                        label = { Text("标题") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 1,
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(10.dp))
                    Column {
                        Text(
                            text = "优先级",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(Modifier.height(6.dp))
                        PriorityChips(selected = priority, onSelect = vm::setPriority)
                    }
                    Spacer(Modifier.height(12.dp))
                    Text(
                        text = "截止时间",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(6.dp))
                    DueEditor(dueAt = dueAt, onDueChange = vm::setDueAt)
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = notes,
                        onValueChange = vm::setNotes,
                        label = { Text("备注") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3,
                        maxLines = 8,
                    )
                    Spacer(Modifier.height(16.dp))

                    ActionButtons(
                        status = current.status,
                        onComplete = vm::toggleDone,
                        onSnooze = vm::snoozeOneHour,
                        onIgnore = vm::ignore,
                        onRestore = vm::restore,
                        onDelete = { confirmDelete = true },
                    )

                    Spacer(Modifier.height(22.dp))
                    ReadOnlyInfo(current)
                    Spacer(Modifier.height(18.dp))
                    EventSection(event)
                    Spacer(Modifier.height(18.dp))
                    SourcesSection(
                        loading = loadingDetail,
                        error = detailError,
                        sources = sources,
                        onRetry = vm::loadDetail,
                        onCopy = { text ->
                            clipboard.setText(AnnotatedString(text))
                            scope.launch { snackbar.showSnackbar("已复制来源原文") }
                        },
                    )
                    Spacer(Modifier.height(32.dp))
                }
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

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("删除任务") },
            text = { Text("删除后电脑端也会同步删除，且无法恢复。确定删除吗？") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    vm.delete()
                    onBack()
                }) {
                    Text("删除", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = false }) { Text("取消") }
            },
        )
    }
}

@Composable
private fun ActionButtons(
    status: String,
    onComplete: () -> Unit,
    onSnooze: () -> Unit,
    onIgnore: () -> Unit,
    onRestore: () -> Unit,
    onDelete: () -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (status == "pending") {
            Button(
                onClick = onComplete,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp),
            ) { Text("完成") }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    onClick = onSnooze,
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 48.dp),
                ) { Text("稍后 1 小时") }
                OutlinedButton(
                    onClick = onIgnore,
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 48.dp),
                ) { Text("忽略") }
            }
        } else {
            Button(
                onClick = onRestore,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp),
            ) { Text("恢复待办") }
        }
        OutlinedButton(
            onClick = onDelete,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp),
            colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.error),
            border = BorderStroke(1.dp, colors.error.copy(alpha = 0.45f)),
        ) { Text("删除任务") }
    }
}

@Composable
private fun ReadOnlyInfo(task: TaskEntity) {
    val colors = MaterialTheme.colorScheme
    Text(
        text = "详细信息",
        style = MaterialTheme.typography.labelLarge,
        fontWeight = FontWeight.SemiBold,
        color = colors.onSurfaceVariant,
    )
    Spacer(Modifier.height(8.dp))
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        InfoRow("状态", STATUS_LABELS[task.status] ?: task.status)
        InfoRow("优先级", PRIORITY_LABELS[task.priority] ?: task.priority)
        InfoRow("所属群", task.groupName?.takeIf { it.isNotBlank() } ?: "手动添加")
        InfoRow("微信来源", "${task.sourceCount} 条原始消息")
        InfoRow("创建时间", formatIso(task.createdAt))
        InfoRow("更新时间", formatIso(task.updatedAt))
        if (task.completedAt != null) InfoRow("完成时间", formatIso(task.completedAt))
        if (task.snoozeUntil != null) InfoRow("稍后到", formatIso(task.snoozeUntil))
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
        Text(
            text = label,
            fontSize = 12.5.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(76.dp),
        )
        Text(
            text = value,
            fontSize = 12.5.sp,
            modifier = Modifier.weight(1f),
        )
    }
}

@Composable
private fun EventSection(event: EventDto?) {
    val title = event?.title?.takeIf { it.isNotBlank() } ?: return
    val colors = MaterialTheme.colorScheme
    Text(
        text = "所属事项",
        style = MaterialTheme.typography.labelLarge,
        fontWeight = FontWeight.SemiBold,
        color = colors.onSurfaceVariant,
    )
    Spacer(Modifier.height(8.dp))
    Surface(
        color = colors.surface,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, colors.outlineVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(
                text = title,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            val summary = event?.summary?.takeIf { it.isNotBlank() }
            if (summary != null) {
                Spacer(Modifier.height(4.dp))
                Text(
                    text = summary,
                    fontSize = 12.5.sp,
                    color = colors.onSurfaceVariant,
                    maxLines = 4,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
private fun SourcesSection(
    loading: Boolean,
    error: String?,
    sources: List<SourceDto>,
    onRetry: () -> Unit,
    onCopy: (String) -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            text = "微信原文",
            style = MaterialTheme.typography.labelLarge,
            fontWeight = FontWeight.SemiBold,
            color = colors.onSurfaceVariant,
        )
        if (sources.isNotEmpty()) {
            Spacer(Modifier.width(6.dp))
            Text(
                text = sources.size.toString(),
                fontSize = 11.sp,
                color = colors.outline,
            )
        }
    }
    Spacer(Modifier.height(8.dp))
    when {
        loading -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            repeat(2) {
                Box(
                    Modifier
                        .fillMaxWidth()
                        .height(64.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .background(colors.surfaceVariant),
                )
            }
        }
        error != null -> Surface(
            color = colors.surfaceVariant,
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = "来源加载失败：$error",
                    fontSize = 12.sp,
                    color = colors.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = onRetry) { Text("重试") }
            }
        }
        sources.isEmpty() -> Text(
            text = "暂无来源原文",
            fontSize = 12.5.sp,
            color = colors.onSurfaceVariant,
        )
        else -> Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            sources.forEach { source ->
                SourceCard(source, onCopy)
            }
        }
    }
}

@Composable
private fun SourceCard(source: SourceDto, onCopy: (String) -> Unit) {
    val colors = MaterialTheme.colorScheme
    val content = source.content?.takeIf { it.isNotBlank() } ?: "（无内容）"
    val meta = buildList {
        source.sender?.takeIf { it.isNotBlank() }?.let { add(it) }
        formatSourceTime(source.ts)?.let { add(it) }
        source.groupName?.takeIf { it.isNotBlank() }?.let { add(it) }
    }.joinToString(" · ")
    Surface(
        color = colors.surface,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, colors.outlineVariant),
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .clickable { onCopy(content) },
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(
                text = content,
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 10,
                overflow = TextOverflow.Ellipsis,
            )
            if (meta.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    text = meta,
                    fontSize = 11.sp,
                    color = colors.onSurfaceVariant,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

private fun formatIso(raw: String?): String {
    val at = TimeUtil.parseOrNull(raw) ?: return raw?.takeIf { it.isNotBlank() } ?: "—"
    return "${TimeUtil.dayLabel(at.toLocalDate())} ${TimeUtil.timeLabel(at)}"
}

private fun formatSourceTime(raw: String?): String? {
    val at = TimeUtil.parseOrNull(raw) ?: return raw?.takeIf { it.isNotBlank() }
    return "${TimeUtil.dayLabel(at.toLocalDate())} ${TimeUtil.timeLabel(at)}"
}
