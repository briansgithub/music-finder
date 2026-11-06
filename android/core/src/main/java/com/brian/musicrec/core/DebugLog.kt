package com.brian.musicrec.core

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** In-memory ring buffer behind the in-app Debug screen (errors, engine calls, info). */
object DebugLog {
    enum class Kind { CALL, ERROR, INFO }

    data class Entry(val ts: Long, val kind: Kind, val message: String, val durationMs: Long? = null) {
        fun format(): String {
            val t = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS Z", Locale.US).format(Date(ts))
            val d = durationMs?.let { " (${it}ms)" } ?: ""
            return "$t [$kind] $message$d"
        }
    }

    private const val MAX = 600
    private val entries = ArrayDeque<Entry>()
    @Volatile var listener: (() -> Unit)? = null

    fun add(kind: Kind, message: String, durationMs: Long? = null) {
        synchronized(entries) {
            entries.addLast(Entry(System.currentTimeMillis(), kind, message.take(2000), durationMs))
            while (entries.size > MAX) entries.removeFirst()
        }
        listener?.invoke()
    }

    fun info(m: String) = add(Kind.INFO, m)
    fun error(m: String, t: Throwable? = null) =
        add(Kind.ERROR, if (t == null) m else "$m: ${t::class.java.simpleName}: ${t.message}\n${t.stackTrace.take(6).joinToString("\n") { "  at $it" }}")

    fun snapshot(): List<Entry> = synchronized(entries) { entries.toList() }
    fun clear() = synchronized(entries) { entries.clear() }

    /** Time a block as an engine CALL; failures are logged as ERROR and rethrown. */
    inline fun <T> call(name: String, block: () -> T): T {
        val t0 = System.nanoTime()
        try {
            val r = block()
            add(Kind.CALL, "$name -> ${summarize(r)}", (System.nanoTime() - t0) / 1_000_000)
            return r
        } catch (e: Exception) {
            add(Kind.ERROR, "$name failed: ${e::class.java.simpleName}: ${e.message}", (System.nanoTime() - t0) / 1_000_000)
            throw e
        }
    }

    fun summarize(r: Any?): String = when (r) {
        null -> "null"
        is Unit -> "ok"
        is Recommendation -> "${r.artist} – ${r.title} [${r.lane}] conf=${r.confidence} vid=${r.videoId.ifEmpty { "-" }} (${r.resolveSource})"
        is DialState -> "lane=${r.lane} chips=${r.availableGenres.size} enabled=${r.enabledGenres?.size ?: "all"}"
        else -> r.toString().take(300)
    }

    fun dump(header: String): String = buildString {
        appendLine(header)
        snapshot().forEach { appendLine(it.format()) }
    }
}
