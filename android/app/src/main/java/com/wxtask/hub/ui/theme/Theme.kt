package com.wxtask.hub.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Ink = Color(0xFF0A0A0A)
val Paper = Color(0xFFFFFFFF)

// 深色：背景 #0E0E0E，卡片 #1A1A1A，文字 #EDEDED
val DarkBg = Color(0xFF0E0E0E)
val DarkSurface = Color(0xFF1A1A1A)
val DarkText = Color(0xFFEDEDED)
val DarkMuted = Color(0xFF9B9BA4)
val DarkBorder = Color(0x1FFFFFFF)

// 浅色：白底黑字（与网页版一致）
val LightBg = Color(0xFFFFFFFF)
val LightSurface = Color(0xFFFFFFFF)
val LightText = Color(0xFF0A0A0A)
val LightMuted = Color(0xFF6B6B73)
val LightBorder = Color(0x14000000)

val Danger = Color(0xFFB42318)
val DangerDark = Color(0xFFFF6B5E)

private val LightColors = lightColorScheme(
    primary = Ink,
    onPrimary = Paper,
    secondary = LightMuted,
    onSecondary = Paper,
    background = LightBg,
    onBackground = LightText,
    surface = LightSurface,
    onSurface = LightText,
    surfaceVariant = Color(0xFFF5F5F5),
    onSurfaceVariant = LightMuted,
    outline = Color(0xFFD9D9D9),
    outlineVariant = Color(0xFFE6E6E6),
    error = Danger,
    onError = Paper,
    scrim = Color(0x66000000),
)

private val DarkColors = darkColorScheme(
    primary = DarkText,
    onPrimary = Color(0xFF0A0A0A),
    secondary = DarkMuted,
    onSecondary = Color(0xFF0A0A0A),
    background = DarkBg,
    onBackground = DarkText,
    surface = DarkSurface,
    onSurface = DarkText,
    surfaceVariant = Color(0xFF202022),
    onSurfaceVariant = DarkMuted,
    outline = Color(0xFF3A3A3C),
    outlineVariant = Color(0xFF2A2A2C),
    error = DangerDark,
    onError = Color(0xFF0A0A0A),
    scrim = Color(0x99000000),
)

@Composable
fun WeChatTaskTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        content = content,
    )
}
