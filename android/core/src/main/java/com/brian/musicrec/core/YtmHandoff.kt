package com.brian.musicrec.core

import android.content.Context
import android.content.Intent
import android.net.Uri

/**
 * Playback handoff to YouTube Music only.
 * Prefer watch?v=VIDEO_ID so YTM starts the exact song immediately.
 * NEVER download or stream YouTube audio inside this app.
 */
object YtmHandoff {
    const val YTM_PACKAGE = "com.google.android.apps.youtube.music"

    fun searchUrl(artist: String, title: String): String {
        val q = Uri.encode("$artist $title".trim())
        return "https://music.youtube.com/search?q=$q"
    }

    fun watchUrl(videoId: String): String? {
        val id = videoId.trim()
        if (id.length != 11) return null
        return "https://music.youtube.com/watch?v=$id"
    }

    /** Open YTM on the exact watch URL when video_id is known. */
    fun open(context: Context, rec: Recommendation) {
        val watch = when {
            rec.videoId.length == 11 -> watchUrl(rec.videoId)
            rec.ytmUrl.contains("watch?v=") -> rec.ytmUrl
            else -> null
        }
        val url = watch
            ?: rec.ytmUrl.takeIf { it.isNotBlank() }
            ?: rec.ytmSearchUrl.ifBlank { searchUrl(rec.artist, rec.title) }

        val ytmIntent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).apply {
            setPackage(YTM_PACKAGE)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(ytmIntent)
        } catch (_: Exception) {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
    }
}
