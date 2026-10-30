package com.wxtask.hub

import android.app.Application
import android.net.ConnectivityManager
import android.net.Network
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class WxTaskApp : Application() {

    lateinit var container: AppContainer
        private set

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        container.notifier.createChannels()
        container.schedulePeriodicSync()
        container.requestImmediateSync("app-start", dedupe = true)
        appScope.launch { container.alarmScheduler.rescheduleAll() }
        registerNetworkMonitor()
    }

    /** 网络恢复（断→连）时立即补同步。 */
    private fun registerNetworkMonitor() {
        val connectivity = getSystemService(ConnectivityManager::class.java) ?: return
        runCatching {
            connectivity.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    container.requestImmediateSync("network", dedupe = true)
                }
            })
        }
    }
}
