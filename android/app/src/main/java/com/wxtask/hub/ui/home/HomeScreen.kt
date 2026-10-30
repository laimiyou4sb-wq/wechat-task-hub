package com.wxtask.hub.ui.home

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.wxtask.hub.AppContainer
import com.wxtask.hub.data.local.SyncStateEntity
import com.wxtask.hub.data.local.TaskEntity
import com.wxtask.hub.util.TimeUtil
import com.wxtask.hub.ui.components.EmptyState
import com.wxtask.hub.ui.components.SectionHeader
import com.wxtask.hub.ui.components.TaskCard

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    container: AppContainer,
    onOpenTask: (String) -> Unit,
    onOpenSettings: () -> Unit,
    onNewTask: () -> Unit,
) {
    val vm: HomeViewModel = viewModel(
        factory = viewModelFactory { initializer { HomeViewModel(container) } },
    )
    val ui by vm.ui.collectAsStateWithLifecycle()
    val settings by vm.settings.collectAsStateWithLifecycle()
    val syncState by vm.syncState.collectAsStateWithLifecycle()
    val syncing by vm.syncing.collectAsStateWithLifecycle()
    val refreshing by vm.refreshing.collectAsStateWithLifecycle()
    val tab by vm.tab.collectAsStateWithLifecycle()
    val archive by vm.archiveFilter.collectAsStateWithLifecycle()
    val outboxCount by vm.outboxCount.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    val context = LocalContext.current

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { }
    LaunchedEffect(settings.paired, settings.notifyPrompted) {
        if (settings.paired && !settings.notifyPrompted && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            vm.markNotificationPrompted()
            permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
    LaunchedEffect(Unit) {
        vm.notices.collect { snackbar.showSnackbar(it) }
    }

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Column(Modifier.fillMaxSize().statusBarsPadding()) {
            HomeHeader(
                paired = settings.paired,
                tokenInvalid = settings.tokenInvalid,
                syncing = syncing,
                refreshing = refreshing,
                syncState = syncState,
                outboxCount = outboxCount,
                onRefresh = vm::refresh,
                onOpenSettings = onOpenSettings,
            )
            HomeTabs(
                selected = tab,
                counts = ui.counts,
                onSelect = vm::selectTab,
            )
            if (tab == HomeTab.DONE) {
                ArchiveFilterRow(
                    selected = archive,
                    counts = ui.counts,
                    onSelect = vm::selectArchive,
                )
            }
            PullToRefreshBox(
                isRefreshing = refreshing,
                onRefresh = vm::refresh,
                modifier = Modifier.weight(1f).fillMaxWidth(),
            ) {
                val emptyCopy = resolveEmptyCopy(tab, settings.paired, settings.tokenInvalid, syncState)
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 2.dp, bottom = 112.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (ui.isEmpty) {
                        item(key = "empty") {
                            Box(Modifier.fillParentMaxSize()) {
                                EmptyState(
                                    title = emptyCopy.title,
                                    text = emptyCopy.text,
                                    actionLabel = emptyCopy.actionLabel,
                                    onAction = when {
                                        emptyCopy.actionLabel == null -> null
                                        !settings.paired -> onOpenSettings
                                        else -> vm::refresh
                                    },
                                )
                            }
                        }
                    } else {
                        ui.sections.forEach { section ->
                            if (section.title != null) {
                                item(key = "header-${section.title}") {
                                    SectionHeader(section.title)
                                }
                            }
                            items(section.tasks, key = { it.id }) { task ->
                                TaskCard(
                                    task = task,
                                    onClick = { onOpenTask(task.id) },
                                    onToggle = { vm.toggleDone(task) },
                                )
                            }
                        }
                    }
                }
            }
        }

        FloatingActionButton(
            onClick = onNewTask,
            containerColor = MaterialTheme.colorScheme.primary,
            contentColor = MaterialTheme.colorScheme.onPrimary,
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .navigationBarsPadding()
                .padding(end = 20.dp, bottom = 22.dp),
        ) {
            Icon(Icons.Filled.Add, contentDescription = "新建任务")
        }

        SnackbarHost(
            hostState = snackbar,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(bottom = 88.dp, start = 16.dp, end = 16.dp),
        )
    }
}

@Composable
private fun HomeHeader(
    paired: Boolean,
    tokenInvalid: Boolean,
    syncing: Boolean,
    refreshing: Boolean,
    syncState: SyncStateEntity?,
    outboxCount: Int,
    onRefresh: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 20.dp, end = 8.dp, top = 10.dp, bottom = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                text = "微信待办",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.height(3.dp))
            SyncStatusLine(
                paired = paired,
                tokenInvalid = tokenInvalid,
                syncing = syncing || refreshing,
                syncState = syncState,
                outboxCount = outboxCount,
            )
        }
        IconButton(onClick = onRefresh, enabled = !refreshing) {
            if (syncing || refreshing) {
                CircularProgressIndicator(
                    modifier = Modifier.size(18.dp),
                    strokeWidth = 2.dp,
                    color = colors.onSurfaceVariant,
                )
            } else {
                Icon(Icons.Filled.Refresh, contentDescription = "刷新", tint = colors.onSurfaceVariant)
            }
        }
        IconButton(onClick = onOpenSettings) {
            Icon(Icons.Filled.Settings, contentDescription = "设置", tint = colors.onSurfaceVariant)
        }
    }
}

@Composable
private fun SyncStatusLine(
    paired: Boolean,
    tokenInvalid: Boolean,
    syncing: Boolean,
    syncState: SyncStateEntity?,
    outboxCount: Int,
) {
    val colors = MaterialTheme.colorScheme
    val (text, tint) = when {
        !paired -> "未配对 · 在设置里粘贴配对码" to colors.onSurfaceVariant
        tokenInvalid -> "令牌失效，请重新配对" to colors.error
        syncing -> "同步中…" to colors.onSurfaceVariant
        syncState?.lastError != null -> "同步失败：${syncState.lastError}" to colors.error
        else -> {
            val base = "已同步 · ${TimeUtil.relativeSyncTime(syncState?.lastSyncAt)}"
            val pending = if (outboxCount > 0) " · $outboxCount 条待上传" else ""
            (base + pending) to colors.onSurfaceVariant
        }
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier
                .size(6.dp)
                .background(
                    when {
                        !paired || tokenInvalid || syncState?.lastError != null -> colors.outline
                        syncing -> colors.onSurfaceVariant
                        else -> colors.onSurface
                    },
                    CircleShape,
                ),
        )
        Spacer(Modifier.width(6.dp))
        Text(
            text = text,
            fontSize = 11.5.sp,
            color = tint,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun HomeTabs(
    selected: HomeTab,
    counts: HomeCounts,
    onSelect: (HomeTab) -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)) {
        HomeTab.entries.forEach { item ->
            val isSelected = selected == item
            val count = when (item) {
                HomeTab.TODAY -> counts.today
                HomeTab.WEEK -> counts.week
                HomeTab.LATER -> counts.later
                HomeTab.DONE -> counts.done
            }
            Column(
                modifier = Modifier
                    .weight(1f)
                    .heightIn(min = 44.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { onSelect(item) }
                    .padding(vertical = 6.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = item.label,
                        fontSize = 13.sp,
                        fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Normal,
                        color = if (isSelected) colors.onSurface else colors.onSurfaceVariant,
                    )
                    Spacer(Modifier.width(4.dp))
                    Text(
                        text = count.toString(),
                        fontSize = 10.5.sp,
                        color = if (isSelected) colors.onSurfaceVariant else colors.outline,
                    )
                }
                Spacer(Modifier.height(4.dp))
                Box(
                    Modifier
                        .height(2.dp)
                        .fillMaxWidth(0.5f)
                        .background(
                            if (isSelected) colors.primary else Color.Transparent,
                            RoundedCornerShape(1.dp),
                        ),
                )
            }
        }
    }
}

@Composable
private fun ArchiveFilterRow(
    selected: ArchiveFilter,
    counts: HomeCounts,
    onSelect: (ArchiveFilter) -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        ArchiveFilter.entries.forEach { item ->
            val isSelected = selected == item
            val count = when (item) {
                ArchiveFilter.DONE -> counts.done
                ArchiveFilter.IGNORED -> counts.ignored
                ArchiveFilter.SNOOZED -> counts.snoozed
            }
            Surface(
                shape = RoundedCornerShape(50),
                color = if (isSelected) colors.primary else colors.surfaceVariant,
                modifier = Modifier
                    .heightIn(min = 44.dp)
                    .clip(RoundedCornerShape(50))
                    .clickable { onSelect(item) },
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = item.label,
                        fontSize = 12.sp,
                        color = if (isSelected) colors.onPrimary else colors.onSurfaceVariant,
                    )
                    Spacer(Modifier.width(4.dp))
                    Text(
                        text = count.toString(),
                        fontSize = 10.5.sp,
                        color = if (isSelected) colors.onPrimary.copy(alpha = 0.7f) else colors.outline,
                    )
                }
            }
        }
    }
}

private data class EmptyCopy(
    val title: String,
    val text: String,
    val actionLabel: String? = null,
)

private fun resolveEmptyCopy(
    tab: HomeTab,
    paired: Boolean,
    tokenInvalid: Boolean,
    syncState: SyncStateEntity?,
): EmptyCopy {
    if (!paired) {
        return EmptyCopy("尚未配对", "在设置里粘贴电脑端配对码，微信待办就会同步到这里。", "去配对")
    }
    if (tokenInvalid) {
        return EmptyCopy("令牌失效", "配对令牌被电脑端拒绝，请重新粘贴配对码。", "重新配对")
    }
    if (syncState?.lastError != null) {
        return EmptyCopy("同步失败", syncState.lastError, "重试")
    }
    return when (tab) {
        HomeTab.TODAY -> EmptyCopy("今天没有待办", "微信里来了新任务，会自动出现在这里。")
        HomeTab.WEEK -> EmptyCopy("本周没有待办", "接下来几天暂时轻松，先歇一会儿。")
        HomeTab.LATER -> EmptyCopy("以后没有待办", "更远期的任务会收在这里。")
        HomeTab.DONE -> EmptyCopy("暂无归档任务", "完成、忽略或稍后的任务会收在这里。")
    }
}

@Suppress("unused")
private fun taskPreview(task: TaskEntity): String = task.title
