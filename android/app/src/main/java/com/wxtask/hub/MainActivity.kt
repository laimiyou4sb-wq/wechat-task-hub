package com.wxtask.hub

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.lifecycleScope
import com.wxtask.hub.ui.AppRoot
import com.wxtask.hub.ui.theme.WeChatTaskTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private val container: AppContainer
        get() = (application as WxTaskApp).container

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleIntent(intent)
        setContent {
            WeChatTaskTheme {
                AppRoot(container)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        container.requestImmediateSync("foreground", dedupe = true)
    }

    /** 处理配对深链（wxtask://pair?code=...）与通知点击带来的任务 id。 */
    private fun handleIntent(intent: Intent?) {
        if (intent == null) return
        val data = intent.data
        if (data != null && data.scheme == "wxtask" && data.host == "pair") {
            val code = data.getQueryParameter("code")
            if (!code.isNullOrBlank()) {
                lifecycleScope.launch {
                    when (val result = container.pairWithCode(code)) {
                        is PairResult.Success -> toast(result.message)
                        is PairResult.Error -> toast(result.message)
                    }
                }
            }
        }
        intent.getStringExtra(EXTRA_OPEN_TASK)?.takeIf { it.isNotBlank() }?.let { container.openTask(it) }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    companion object {
        const val EXTRA_OPEN_TASK = "open_task"
    }
}
