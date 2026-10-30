package com.wxtask.hub.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wxtask.hub.AppContainer
import com.wxtask.hub.PairResult
import com.wxtask.hub.data.local.SyncStateEntity
import com.wxtask.hub.data.prefs.AppSettings
import com.wxtask.hub.data.remote.ApiResult
import com.wxtask.hub.data.remote.PingDto
import com.wxtask.hub.sync.SyncOutcome
import com.wxtask.hub.util.TimeUtil
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

data class OpMessage(val text: String, val isError: Boolean = false)

class SettingsViewModel(private val container: AppContainer) : ViewModel() {

    val settings = container.settingsStore.settings
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), AppSettings())

    val syncState: kotlinx.coroutines.flow.StateFlow<SyncStateEntity?> = container.db.syncStateDao().observe()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val syncing = container.syncEngine.syncing

    val outboxCount = container.repository.observeOutboxCount()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val taskCount = container.repository.observeTasks()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList<com.wxtask.hub.data.local.TaskEntity>())

    val appVersion: String = container.appVersionName()

    val pairingBusy = MutableStateFlow(false)
    val pairingResult = MutableStateFlow<OpMessage?>(null)
    val testingAll = MutableStateFlow(false)
    val endpointResults = MutableStateFlow<Map<String, OpMessage>>(emptyMap())
    val serverInfo = MutableStateFlow<String?>(null)
    val syncResult = MutableStateFlow<OpMessage?>(null)

    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val notices = _notices

    init {
        refreshServerInfo()
    }

    fun pairWithCode(code: String) {
        if (code.isBlank()) {
            pairingResult.value = OpMessage("请先粘贴配对码", true)
            return
        }
        viewModelScope.launch {
            pairingBusy.value = true
            when (val result = container.pairWithCode(code)) {
                is PairResult.Success -> {
                    pairingResult.value = OpMessage(result.message)
                    refreshServerInfo()
                }
                is PairResult.Error -> pairingResult.value = OpMessage(result.message, true)
            }
            pairingBusy.value = false
        }
    }

    fun pairManual(url: String, token: String) {
        viewModelScope.launch {
            pairingBusy.value = true
            when (val result = container.pairManual(url, token)) {
                is PairResult.Success -> {
                    pairingResult.value = OpMessage(result.message)
                    refreshServerInfo()
                }
                is PairResult.Error -> pairingResult.value = OpMessage(result.message, true)
            }
            pairingBusy.value = false
        }
    }

    fun testEndpoint(url: String) {
        val token = settings.value.token
        if (token.isBlank()) {
            endpointResults.value = endpointResults.value + (url to OpMessage("尚未配对，先填写 token", true))
            return
        }
        endpointResults.value = endpointResults.value + (url to OpMessage("测试中…"))
        viewModelScope.launch {
            val result = container.testEndpoint(url, token)
            endpointResults.value = endpointResults.value + (url to describePing(result))
        }
    }

    fun testAllEndpoints() {
        if (testingAll.value) return
        val current = settings.value
        if (current.token.isBlank() || current.endpoints.isEmpty()) {
            _notices.tryEmit("尚未配对，没有可测试的地址")
            return
        }
        viewModelScope.launch {
            testingAll.value = true
            current.endpoints.forEach { endpoint ->
                endpointResults.value = endpointResults.value + (endpoint.url to OpMessage("测试中…"))
                val result = container.testEndpoint(endpoint.url, current.token)
                endpointResults.value = endpointResults.value + (endpoint.url to describePing(result))
            }
            testingAll.value = false
        }
    }

    fun syncNow() {
        if (syncing.value) return
        viewModelScope.launch {
            val outcome = container.syncNow()
            syncResult.value = when (outcome) {
                is SyncOutcome.Success -> OpMessage(
                    if (outcome.newTaskCount > 0) "同步完成，新增 ${outcome.newTaskCount} 条待办" else "同步完成",
                )
                is SyncOutcome.Failed -> OpMessage(outcome.message, true)
                SyncOutcome.Unauthorized -> OpMessage("令牌失效，请重新配对", true)
                SyncOutcome.NotPaired -> OpMessage("尚未配对，请先粘贴配对码", true)
            }
        }
    }

    fun setNotifyNew(enabled: Boolean) {
        viewModelScope.launch { container.settingsStore.setNotifyNew(enabled) }
    }

    fun setNotifyDue(enabled: Boolean) {
        viewModelScope.launch { container.settingsStore.setNotifyDue(enabled) }
    }

    fun refreshServerInfo() {
        viewModelScope.launch {
            when (val result = container.pingServer()) {
                is ApiResult.Success -> {
                    val version = result.data.version ?: "?"
                    val time = TimeUtil.relativeSyncTime(result.data.serverTime)
                    serverInfo.value = "v$version · $time"
                }
                is ApiResult.Unauthorized -> serverInfo.value = "token 无效（401）"
                is ApiResult.NotFound -> serverInfo.value = "接口不存在（电脑端版本过旧）"
                is ApiResult.Failure -> serverInfo.value = result.message
            }
        }
    }

    fun clearPairing() {
        viewModelScope.launch {
            container.settingsStore.clearPairing()
            container.taskStore.clearAll()
            container.alarmScheduler.rescheduleAll()
            pairingResult.value = OpMessage("已取消配对，本地缓存已清空")
            serverInfo.value = null
        }
    }

    private fun describePing(result: ApiResult<PingDto>): OpMessage = when (result) {
        is ApiResult.Success -> {
            val version = result.data.version ?: "?"
            val time = TimeUtil.relativeSyncTime(result.data.serverTime)
            OpMessage("连接正常 · v$version · $time")
        }
        is ApiResult.Unauthorized -> OpMessage("token 无效（401）", true)
        is ApiResult.NotFound -> OpMessage("接口不存在（电脑端版本过旧）", true)
        is ApiResult.Failure -> OpMessage(result.message, true)
    }
}
