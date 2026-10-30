package com.wxtask.hub.ui.settings

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.wxtask.hub.AppContainer
import com.wxtask.hub.data.prefs.Endpoint
import com.wxtask.hub.ui.components.ScreenTopBar
import com.wxtask.hub.util.HyperOs
import com.wxtask.hub.util.TimeUtil

private const val AUTO_START_GUIDE =
    "系统页面没有打开。请手动进入：设置 → 应用设置 → 应用管理 → 微信待办 → 自启动，允许自启动。"

private const val BATTERY_GUIDE =
    "系统页面没有打开。请手动进入：设置 → 应用设置 → 应用管理 → 微信待办 → 省电策略，选择「无限制」。"

private const val ALARM_GUIDE =
    "系统页面没有打开。请手动进入：设置 → 应用设置 → 应用管理 → 微信待办 → 其他权限 → 闹钟和提醒，允许。"

@Composable
fun SettingsScreen(container: AppContainer, onBack: () -> Unit) {
    val vm: SettingsViewModel = viewModel(
        factory = viewModelFactory { initializer { SettingsViewModel(container) } },
    )
    val settings by vm.settings.collectAsStateWithLifecycle()
    val syncState by vm.syncState.collectAsStateWithLifecycle()
    val syncing by vm.syncing.collectAsStateWithLifecycle()
    val outboxCount by vm.outboxCount.collectAsStateWithLifecycle()
    val localTasks by vm.taskCount.collectAsStateWithLifecycle()
    val pairingBusy by vm.pairingBusy.collectAsStateWithLifecycle()
    val pairingResult by vm.pairingResult.collectAsStateWithLifecycle()
    val testingAll by vm.testingAll.collectAsStateWithLifecycle()
    val endpointResults by vm.endpointResults.collectAsStateWithLifecycle()
    val serverInfo by vm.serverInfo.collectAsStateWithLifecycle()
    val syncResult by vm.syncResult.collectAsStateWithLifecycle()

    val context = LocalContext.current
    val snackbar = remember { SnackbarHostState() }
    var pairingCode by remember { mutableStateOf("") }
    var manualUrl by remember { mutableStateOf("") }
    var manualToken by remember { mutableStateOf("") }
    var showManual by remember { mutableStateOf(false) }
    var showDebug by remember { mutableStateOf(false) }
    var confirmClear by remember { mutableStateOf(false) }
    var guideMessage by remember { mutableStateOf<String?>(null) }
    var statusTick by remember { mutableStateOf(0) }

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) statusTick++
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    val notificationsEnabled = remember(statusTick) {
        NotificationManagerCompat.from(context).areNotificationsEnabled()
    }
    val exactAlarmAllowed = remember(statusTick) { HyperOs.canScheduleExactAlarms(context) }
    val batteryUnrestricted = remember(statusTick) { HyperOs.isIgnoringBatteryOptimizations(context) }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { statusTick++ }

    LaunchedEffect(Unit) {
        vm.notices.collect { snackbar.showSnackbar(it) }
    }

    Box(Modifier.fillMaxSize()) {
        Column(
            Modifier
                .fillMaxSize()
                .statusBarsPadding(),
        ) {
            ScreenTopBar(title = "设置", onBack = onBack)
            LazyColumn(
                modifier = Modifier
                    .weight(1f)
                    .navigationBarsPadding(),
                contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 32.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                item {
                    SettingsCard("配对") {
                        Text(
                            text = if (settings.paired) {
                                "已配对：${settings.deviceName.ifBlank { "电脑端" }}"
                            } else {
                                "尚未配对"
                            },
                            style = MaterialTheme.typography.bodyMedium,
                            fontWeight = FontWeight.Medium,
                        )
                        InfoLine(
                            "当前地址",
                            syncState?.activeUrl
                                ?: settings.endpoints.firstOrNull()?.url
                                ?: "—",
                        )
                        Text(
                            text = "地址自动切换：手机与电脑在同一网段（含 USB 共享网络 / 热点）时优先局域网直连；" +
                                "不在同一网络时自动走远程地址（ngrok）。连接失败会自动切到下一个地址。",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        OutlinedTextField(
                            value = pairingCode,
                            onValueChange = { pairingCode = it },
                            label = { Text("配对码") },
                            placeholder = { Text("粘贴电脑端设置页里的配对码") },
                            modifier = Modifier.fillMaxWidth(),
                            minLines = 2,
                            maxLines = 4,
                        )
                        Button(
                            onClick = { vm.pairWithCode(pairingCode) },
                            enabled = !pairingBusy && pairingCode.isNotBlank(),
                            modifier = Modifier
                                .fillMaxWidth()
                                .heightIn(min = 46.dp),
                        ) {
                            if (pairingBusy) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    strokeWidth = 2.dp,
                                    color = MaterialTheme.colorScheme.onPrimary,
                                )
                                Spacer(Modifier.width(8.dp))
                            }
                            Text("配对并同步")
                        }
                        pairingResult?.let { message ->
                            Text(
                                text = message.text,
                                fontSize = 12.sp,
                                color = if (message.isError) {
                                    MaterialTheme.colorScheme.error
                                } else {
                                    MaterialTheme.colorScheme.onSurfaceVariant
                                },
                            )
                        }
                        TextButton(
                            onClick = { showManual = !showManual },
                            modifier = Modifier.heightIn(min = 40.dp),
                        ) {
                            Text(if (showManual) "收起手动配对" else "手动填地址 + token")
                        }
                        if (showManual) {
                            OutlinedTextField(
                                value = manualUrl,
                                onValueChange = { manualUrl = it },
                                label = { Text("电脑端地址") },
                                placeholder = { Text("http://192.168.1.20:8765") },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                            )
                            OutlinedTextField(
                                value = manualToken,
                                onValueChange = { manualToken = it },
                                label = { Text("token") },
                                placeholder = { Text("wt-...") },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                visualTransformation = PasswordVisualTransformation(),
                            )
                            OutlinedButton(
                                onClick = { vm.pairManual(manualUrl, manualToken) },
                                enabled = !pairingBusy && manualUrl.isNotBlank() && manualToken.isNotBlank(),
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .heightIn(min = 46.dp),
                            ) { Text("保存并配对") }
                        }
                        if (settings.endpoints.isNotEmpty()) {
                            HorizontalDivider()
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    text = "地址列表",
                                    fontSize = 12.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.weight(1f),
                                )
                                TextButton(
                                    onClick = vm::testAllEndpoints,
                                    enabled = !testingAll,
                                    modifier = Modifier.heightIn(min = 40.dp),
                                ) { Text(if (testingAll) "测试中…" else "测试全部") }
                            }
                            settings.endpoints.forEach { endpoint ->
                                EndpointRow(
                                    endpoint = endpoint,
                                    result = endpointResults[endpoint.url],
                                    onTest = { vm.testEndpoint(endpoint.url) },
                                )
                            }
                        }
                    }
                }

                item {
                    SettingsCard("同步") {
                        InfoLine("上次同步", TimeUtil.relativeSyncTime(syncState?.lastSyncAt))
                        InfoLine("待上传操作", "$outboxCount 条")
                        InfoLine("本地任务", "${localTasks.size} 条")
                        syncState?.lastError?.let { error ->
                            Text(
                                text = "上次错误：$error",
                                fontSize = 12.sp,
                                color = MaterialTheme.colorScheme.error,
                            )
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(
                                onClick = vm::syncNow,
                                enabled = !syncing,
                                modifier = Modifier
                                    .weight(1f)
                                    .heightIn(min = 46.dp),
                            ) {
                                if (syncing) {
                                    CircularProgressIndicator(
                                        modifier = Modifier.size(16.dp),
                                        strokeWidth = 2.dp,
                                        color = MaterialTheme.colorScheme.onPrimary,
                                    )
                                    Spacer(Modifier.width(8.dp))
                                }
                                Text(if (syncing) "同步中…" else "立即同步")
                            }
                            OutlinedButton(
                                onClick = vm::refreshServerInfo,
                                modifier = Modifier
                                    .weight(1f)
                                    .heightIn(min = 46.dp),
                            ) { Text("测试连接") }
                        }
                        syncResult?.let { message ->
                            Text(
                                text = message.text,
                                fontSize = 12.sp,
                                color = if (message.isError) {
                                    MaterialTheme.colorScheme.error
                                } else {
                                    MaterialTheme.colorScheme.onSurfaceVariant
                                },
                            )
                        }
                        TextButton(
                            onClick = { showDebug = !showDebug },
                            modifier = Modifier.heightIn(min = 40.dp),
                        ) { Text(if (showDebug) "收起调试信息" else "显示调试信息") }
                        if (showDebug) {
                            InfoLine("cursor", (syncState?.cursor ?: 0L).toString())
                            InfoLine("活跃地址", syncState?.activeUrl ?: "—")
                            InfoLine("地址数量", settings.endpoints.size.toString())
                            InfoLine("token", maskToken(settings.token))
                        }
                    }
                }

                item {
                    SettingsCard("通知") {
                        SwitchRow(
                            title = "新待办汇总通知",
                            checked = settings.notifyNew,
                            onCheckedChange = vm::setNotifyNew,
                        )
                        SwitchRow(
                            title = "到点提醒",
                            checked = settings.notifyDue,
                            onCheckedChange = vm::setNotifyDue,
                        )
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                text = "通知权限：${if (notificationsEnabled) "已开启" else "未开启"}",
                                fontSize = 12.sp,
                                color = if (notificationsEnabled) {
                                    MaterialTheme.colorScheme.onSurfaceVariant
                                } else {
                                    MaterialTheme.colorScheme.error
                                },
                                modifier = Modifier.weight(1f),
                            )
                            if (!notificationsEnabled && Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                                ContextCompatCheck(context)
                            ) {
                                TextButton(
                                    onClick = { permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) },
                                    modifier = Modifier.heightIn(min = 40.dp),
                                ) { Text("去开启") }
                            }
                        }
                    }
                }

                item {
                    SettingsCard("系统权限（小米 / HyperOS）") {
                        Text(
                            text = "为保证后台同步和到点提醒可靠，建议开启以下权限。系统页面打不开时，按提示文字手动设置。",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        GuideRow(
                            title = "自启动",
                            status = "",
                            actionLabel = "去设置",
                            onAction = {
                                if (!HyperOs.openAutoStart(context)) guideMessage = AUTO_START_GUIDE
                            },
                        )
                        GuideRow(
                            title = "省电策略",
                            status = if (batteryUnrestricted) "已设为无限制" else "可能被系统限制",
                            actionLabel = "去设置",
                            onAction = {
                                if (!HyperOs.openBatteryUnrestricted(context)) {
                                    guideMessage = BATTERY_GUIDE
                                } else {
                                    statusTick++
                                }
                            },
                        )
                        GuideRow(
                            title = "精确闹钟",
                            status = if (exactAlarmAllowed) "已允许" else "未允许，提醒可能延后",
                            actionLabel = "去设置",
                            onAction = {
                                if (!HyperOs.openExactAlarmSettings(context)) guideMessage = ALARM_GUIDE
                            },
                        )
                        guideMessage?.let { message ->
                            Surface(
                                color = MaterialTheme.colorScheme.surfaceVariant,
                                shape = RoundedCornerShape(8.dp),
                                modifier = Modifier.fillMaxWidth(),
                            ) {
                                Text(
                                    text = message,
                                    fontSize = 12.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.padding(10.dp),
                                )
                            }
                        }
                    }
                }

                item {
                    SettingsCard("关于") {
                        InfoLine("应用版本", vm.appVersion)
                        InfoLine("电脑端", serverInfo ?: "未连接")
                        InfoLine("配对设备", settings.deviceName.ifBlank { "—" })
                        InfoLine("同步协议", "v1 · /api/app")
                        if (settings.paired) {
                            TextButton(
                                onClick = { confirmClear = true },
                                modifier = Modifier.heightIn(min = 44.dp),
                            ) {
                                Text("取消配对", color = MaterialTheme.colorScheme.error)
                            }
                        }
                    }
                }
            }
        }

        SnackbarHost(
            hostState = snackbar,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(16.dp),
        )
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("取消配对") },
            text = { Text("取消后会清空本地任务缓存，重新配对即可恢复。确定吗？") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    vm.clearPairing()
                }) { Text("取消配对", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { confirmClear = false }) { Text("再想想") }
            },
        )
    }
}

private fun ContextCompatCheck(context: android.content.Context): Boolean =
    androidx.core.content.ContextCompat.checkSelfPermission(
        context,
        Manifest.permission.POST_NOTIFICATIONS,
    ) != PackageManager.PERMISSION_GRANTED

private fun maskToken(token: String): String {
    if (token.isBlank()) return "—"
    return if (token.length <= 8) "****" else "${token.take(4)}…${token.takeLast(2)}"
}

@Composable
private fun SettingsCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    val colors = MaterialTheme.colorScheme
    Column {
        Text(
            text = title,
            style = MaterialTheme.typography.labelLarge,
            fontWeight = FontWeight.SemiBold,
            color = colors.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, bottom = 6.dp),
        )
        Surface(
            color = colors.surface,
            shape = RoundedCornerShape(16.dp),
            border = BorderStroke(1.dp, colors.outlineVariant),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(
                modifier = Modifier.padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
                content = content,
            )
        }
    }
}

@Composable
private fun InfoLine(label: String, value: String) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
        Text(
            text = label,
            fontSize = 12.5.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(88.dp),
        )
        Text(
            text = value,
            fontSize = 12.5.sp,
            modifier = Modifier.weight(1f),
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun SwitchRow(title: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(title, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

@Composable
private fun GuideRow(
    title: String,
    status: String,
    actionLabel: String,
    onAction: () -> Unit,
) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 13.sp)
            if (status.isNotBlank()) {
                Text(
                    text = status,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        TextButton(onClick = onAction, modifier = Modifier.heightIn(min = 40.dp)) {
            Text(actionLabel)
        }
    }
}

@Composable
private fun EndpointRow(endpoint: Endpoint, result: OpMessage?, onTest: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Surface(
                color = colors.surfaceVariant,
                shape = RoundedCornerShape(5.dp),
            ) {
                Text(
                    text = endpoint.kindLabel,
                    fontSize = 10.sp,
                    color = colors.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }
            Spacer(Modifier.width(8.dp))
            Text(
                text = endpoint.url,
                fontSize = 12.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = onTest, modifier = Modifier.heightIn(min = 40.dp)) {
                Text("测试", fontSize = 12.sp)
            }
        }
        result?.let { message ->
            Text(
                text = message.text,
                fontSize = 11.sp,
                color = if (message.isError) colors.error else colors.onSurfaceVariant,
                modifier = Modifier.padding(start = 6.dp, bottom = 4.dp),
            )
        }
    }
}

@Composable
private fun HorizontalDivider() {
    Box(
        Modifier
            .fillMaxWidth()
            .height(1.dp)
            .padding(vertical = 0.dp),
    ) {
        Surface(
            color = MaterialTheme.colorScheme.outlineVariant,
            modifier = Modifier
                .fillMaxWidth()
                .height(1.dp),
        ) {}
    }
}
