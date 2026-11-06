package com.brian.musicrec.core.engine

/** Port of `musicrec.tags.sanitize`. */
object Sanitize {
    val GENERIC_ANCHORS = setOf(
        "album", "audio", "channel", "hits", "mix", "music", "official", "playlist",
        "radio", "records", "release", "songs", "topic", "unknown", "various", "v.a.",
        "va", "vevo", "video",
    )
    val MOOD_TAGS = setOf(
        "aggressive", "calm", "chill", "dark", "energetic", "focus", "happy",
        "melancholic", "party", "peaceful", "relaxing", "sad", "upbeat",
    )
    val KNOWN_GENRES = setOf(
        "acoustic", "ambient", "baroque", "chiptune", "classical", "dance",
        "edm", "electronic", "folk", "funk", "game", "hip hop", "house",
        "indie", "instrumental", "jazz", "metal", "orchestra", "piano",
        "pop", "prog", "punk", "ragtime", "rock", "romantic", "score",
        "soundtrack", "soul", "synth", "trance", "vocal", "ost",
    )

    private val PLAYLIST_TAG_RE = Py.regex(
        "\\b(playlist|official\\s+audio|official\\s+video|\\btopic\\b|radio\\s+hits|" +
            "greatest\\s+hits|best\\s+of|full\\s+album)\\b",
    )
    private val ANIMAL_OR_BABY_MUSIC_RE = Py.regex("\\b(dog|cat|pet|baby|kids|sleep|rain|nature)\\s+music\\b")
    private val PLAYLIST_SHAPE_RE = Py.regex(
        "^(?:lofi|lo-fi|lo fi|fantasy|relaxing|peaceful|epic|sad|happy|chill|study)\\s+" +
            "(?:keys|beats|vibes|tunes|piano|mix|radio|hours|music)$",
    )
    private val CHANNEL_SUFFIX_RE = Py.regex("(?:\\s+-\\s+topic|\\s+topic|\\s+official|\\s+vevo)$")

    private fun norm(name: String?) = Py.collapseWs((name ?: "").trim())

    fun looksLikePlaylistOrChannel(name: String?): Boolean {
        val cf = Py.casefold(norm(name))
        if (cf.isEmpty()) return false
        return Py.search(PLAYLIST_TAG_RE, cf) || Py.search(ANIMAL_OR_BABY_MUSIC_RE, cf) ||
            Py.search(PLAYLIST_SHAPE_RE, cf) || Py.search(CHANNEL_SUFFIX_RE, cf)
    }

    fun isPlausibleArtist(name: String?): Boolean {
        val n = norm(name)
        if (n.codePointCount(0, n.length) < 2) return false
        val cf = Py.casefold(n)
        if (cf in GENERIC_ANCHORS) return false
        if (looksLikePlaylistOrChannel(n)) return false
        val parts = Py.collapseWs(cf).split(' ').filter { it.isNotEmpty() }
        if (parts.size == 2 && parts[1] == "music" &&
            parts[0] in setOf("dog", "cat", "pet", "baby", "kids", "sleep", "rain", "nature")
        ) return false
        return true
    }

    fun isVocabEligible(tag: String?, kind: String = "tag"): Boolean {
        val n = norm(tag)
        if (n.isEmpty()) return false
        if (Py.casefold(n) in GENERIC_ANCHORS) return false
        if (looksLikePlaylistOrChannel(n)) return false
        if (kind == "artist") return isPlausibleArtist(n)
        return true
    }

    fun inferTagKind(tag: String?, requested: String = "tag"): String {
        val req = Py.casefold((requested.ifEmpty { "tag" }).trim()).ifEmpty { "tag" }
        val n = norm(tag)
        if (n.isEmpty() || !isVocabEligible(n, if (req != "tag") req else "tag")) return "junk"
        if (req == "artist" || req == "label") {
            return if (req != "artist" || isPlausibleArtist(n)) req else "junk"
        }
        if (req == "genre" || req == "mood") return req
        val cf = Py.casefold(n)
        if (cf in MOOD_TAGS) return "mood"
        if (cf in KNOWN_GENRES) return "genre"
        return "tag"
    }

    fun isDialWorthy(tag: String?, kind: String = "tag"): Boolean {
        val k = inferTagKind(tag, kind)
        if (k == "junk") return false
        if (k == "artist") return isPlausibleArtist(tag)
        if (looksLikePlaylistOrChannel(tag)) return false
        return true
    }

    fun sanitizeRelatedArtists(names: List<String>?): List<String> {
        val seen = HashSet<String>()
        val out = ArrayList<String>()
        for (raw in names.orEmpty()) {
            val n = norm(raw)
            if (n.isEmpty()) continue
            val key = Py.casefold(n)
            if (key in seen) continue
            if (!isPlausibleArtist(n)) continue
            seen += key
            out += n
        }
        return out
    }
}

/** Port of `musicrec.seeds.filters` (title junk filters + artist skip list). */
object TitleFilters {
    data class Classification(val action: String, val reasons: List<String>)

    private val HARD_DROP = listOf(
        "\\b1\\s*hours?\\b|\\bone\\s*hours?\\b|\\b10\\s*hours?\\b|\\b\\d+\\s*hours?\\b" to "duration_keyword_hours",
        "小时|小時|个小时|個小時" to "duration_keyword_hours_cjk",
        "\\bfull\\s*album\\b|\\balbum\\s*full\\b|完整专辑|完整專輯|全专辑|全專輯" to "full_album",
        "\\bost\\s*full\\b|\\bfull\\s*ost\\b|\\bfull\\s*soundtrack\\b|完整.*?ost|ost.*?合集" to "ost_full",
        "\\bcompilation\\b|\\bcompilations\\b|精选集|精選集|合集" to "compilation",
        "\\bplaylist\\b|播放列表|歌单|歌單" to "playlist",
        "\\breaction\\b|\\breacts?\\b|反应|反應" to "reaction",
        "\\btutorial\\b|\\bhow\\s*to\\s*play\\b|\\blesson\\b|教程|教学|教學" to "tutorial",
        "\\b\\d+\\s*hour\\s+(?:piano|music|mix|sleep|study|relax)" to "hours_program",
        "\\bnature\\s+sounds?\\b" to "nature_sounds",
        "\\brain\\s+sounds?\\b|\\bocean\\s+waves?\\b|\\bwhite\\s+noise\\b" to "ambient_noise",
        (
            "\\bbaby\\s+(?:classical|lullaby|sleep|music|songs?)\\b|" +
                "\\bclassical\\s+for\\s+bab(?:y|ies)\\b|" +
                "\\bfor\\s+babies\\b"
            ) to "baby_music",
        "\\bsleep\\s+(?:music|sounds?|piano)\\b" to "sleep_program",
    ).map { Py.regex(it.first) to it.second }

    private val AMBIGUOUS = listOf(
        "\\bmix\\b|混音" to "mix",
        "\\bextended\\b|\\bextended\\s*version\\b|加长|加長" to "extended",
        "\\bloop\\b|\\blooped\\b|循环|循環" to "loop",
        "\\bmedley\\b|串烧|串燒" to "medley",
        "\\blive\\b|\\blive\\s*at\\b|\\blive\\s*from\\b|现场|現場|演唱会|演唱會" to "live",
        "\\bfreestyle\\b" to "freestyle",
        "\\bconcert\\b|音乐会|音樂會" to "concert",
        "\\bcover\\b|\\bcovers\\b|翻唱|カバー" to "cover",
    ).map { Py.regex(it.first) to it.second }

    private val BROKEN_TITLE_RE = Py.regex("^https?://|Products:|Why is this here\\?")
    private val YT_ID_ONLY = Py.regex("yt:[\\w-]{6,}", ignoreCase = false)
    private val DJ_MIX_TAG = Py.regex("\\(\\s*(original|extended|club|radio|instrumental|official)\\s*mix\\s*\\)")
    private val RADIO_EDIT = Py.regex("\\(\\s*radio\\s*edit\\s*\\)")

    fun classify(title: String?, @Suppress("UNUSED_PARAMETER") artist: String? = ""): Classification {
        val t = title ?: ""
        if (t.trim().isEmpty()) return Classification("drop", listOf("empty_title"))
        if (Py.search(BROKEN_TITLE_RE, t)) return Classification("drop", listOf("broken_title_parse"))
        if (t.startsWith("yt:") && YT_ID_ONLY.matcher(t).matches()) {
            return Classification("review", listOf("playlist_id_only"))
        }
        val reasons = ArrayList<String>()
        for ((p, reason) in HARD_DROP) if (Py.search(p, t)) reasons += reason
        if (reasons.isNotEmpty()) return Classification("drop", reasons)
        var cleaned = DJ_MIX_TAG.matcher(t).replaceAll(" ")
        cleaned = RADIO_EDIT.matcher(cleaned).replaceAll(" ")
        var action = "ok"
        for ((p, reason) in AMBIGUOUS) {
            if (Py.search(p, cleaned)) {
                reasons += reason
                action = "review"
            }
        }
        return Classification(action, reasons)
    }

    /** `artist_skipped`: case-insensitive exact match, or "Name - Topic"-style prefix. */
    fun artistSkipped(artist: String?, skipNames: Collection<String>): Boolean {
        val a = Py.casefold(artist).trim()
        for (name in skipNames) {
            val n = Py.casefold(name).trim()
            if (a == n) return true
            if (a.startsWith("$n ")) return true
        }
        return false
    }
}

/** Port of `engine/clusters.py`. */
object Clusters {
    val TAG_TO_CLUSTER: LinkedHashMap<String, String> = linkedMapOf(
        "classical" to "classical", "baroque" to "classical", "romantic" to "classical",
        "orchestra" to "classical", "piano" to "piano", "ragtime" to "ragtime",
        "soundtrack" to "soundtrack", "score" to "soundtrack", "ost" to "soundtrack",
        "game" to "game", "video game" to "game", "chiptune" to "game",
        "electronic" to "electronic", "edm" to "electronic", "synth" to "electronic",
        "house" to "electronic", "trance" to "trance", "metal" to "metal", "prog" to "metal",
        "pop" to "pop", "jazz" to "jazz", "ambient" to "ambient", "folk" to "folk",
        "vocal" to "vocal", "instrumental" to "instrumental",
    )

    fun assign(tags: List<String>, artist: String = "", title: String = ""): String {
        val blob = Py.casefold((tags + listOf(artist, title)).joinToString(" "))
        for ((needle, cluster) in TAG_TO_CLUSTER) if (blob.contains(needle)) return cluster
        return "other"
    }
}
