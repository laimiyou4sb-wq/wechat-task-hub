package com.wxtask.hub.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.wxtask.hub.AppContainer
import com.wxtask.hub.ui.detail.DetailScreen
import com.wxtask.hub.ui.home.HomeScreen
import com.wxtask.hub.ui.newtask.NewTaskScreen
import com.wxtask.hub.ui.settings.SettingsScreen

@Composable
fun AppRoot(container: AppContainer) {
    val navController = rememberNavController()
    val snackbar = remember { SnackbarHostState() }
    val openTaskId by container.pendingOpenTask.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) {
        container.syncEngine.notices.collect { snackbar.showSnackbar(it) }
    }
    LaunchedEffect(openTaskId) {
        val id = openTaskId
        if (!id.isNullOrBlank()) {
            navController.navigate("detail/$id") { launchSingleTop = true }
            container.consumeOpenTask()
        }
    }

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        NavHost(navController = navController, startDestination = "home") {
            composable("home") {
                HomeScreen(
                    container = container,
                    onOpenTask = { taskId -> navController.navigate("detail/$taskId") },
                    onOpenSettings = { navController.navigate("settings") },
                    onNewTask = { navController.navigate("new") },
                )
            }
            composable(
                route = "detail/{taskId}",
                arguments = listOf(navArgument("taskId") { type = NavType.StringType }),
            ) { entry ->
                val taskId = entry.arguments?.getString("taskId").orEmpty()
                DetailScreen(
                    container = container,
                    taskId = taskId,
                    onBack = { navController.popBackStack() },
                )
            }
            composable("settings") {
                SettingsScreen(
                    container = container,
                    onBack = { navController.popBackStack() },
                )
            }
            composable("new") {
                NewTaskScreen(
                    container = container,
                    onBack = { navController.popBackStack() },
                    onCreated = { navController.popBackStack() },
                )
            }
        }

        SnackbarHost(
            hostState = snackbar,
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding()
                .padding(horizontal = 16.dp, vertical = 12.dp),
        )
    }
}
