package com.brian.musicrec.core.engine

import java.math.BigDecimal
import java.math.RoundingMode
import java.util.Locale
import java.util.regex.Pattern

/**
 * Small helpers that reproduce Python semantics the engine relies on, so the
 * Kotlin port ranks exactly like `recommender/musicrec` (see parity tests).
 */
object Py {
    /** Python `round(x, n)`: exact binary value, ties to even. */
    fun round(x: Double, n: Int): Double {
        if (x.isNaN() || x.isInfinite()) return x
        return BigDecimal(x).setScale(n, RoundingMode.HALF_EVEN).toDouble()
    }

    /** Close-enough `str.casefold()` (lowercase + the common full foldings). */
    fun casefold(s: String?): String {
        if (s.isNullOrEmpty()) return ""
        val lower = s.lowercase(Locale.ROOT)
        if (lower.none { it == 'ß' || it == 'ς' || it == 'ﬁ' || it == 'ﬂ' }) return lower
        return lower.replace("ß", "ss").replace('ς', 'σ').replace("ﬁ", "fi").replace("ﬂ", "fl")
    }

    /** Python `str.isalnum()` for a single code point. */
    fun isAlnum(cp: Int): Boolean {
        if (Character.isLetterOrDigit(cp)) return true
        return when (Character.getType(cp).toByte()) {
            Character.LETTER_NUMBER, Character.OTHER_NUMBER -> true
            else -> false
        }
    }

    /** Python `str.isspace()` for a single code point. */
    fun isSpace(cp: Int): Boolean =
        Character.isWhitespace(cp) || Character.isSpaceChar(cp) || cp == 0x85 ||
            (cp in 0x1c..0x1f)

    /** `" ".join(s.split())` */
    fun collapseWs(s: String?): String {
        if (s.isNullOrEmpty()) return ""
        val out = StringBuilder()
        var pendingSpace = false
        var i = 0
        while (i < s.length) {
            val cp = s.codePointAt(i)
            if (isSpace(cp)) {
                pendingSpace = out.isNotEmpty()
            } else {
                if (pendingSpace) out.append(' ')
                pendingSpace = false
                out.appendCodePoint(cp)
            }
            i += Character.charCount(cp)
        }
        return out.toString()
    }

    /**
     * Compile like Python `re.compile(p, re.I)` on str: Unicode-aware `\w`/`\b`.
     * Android's ICU regex is Unicode-aware by default and may reject
     * UNICODE_CHARACTER_CLASS, so fall back gracefully.
     */
    fun regex(p: String, ignoreCase: Boolean = true): Pattern {
        val base = if (ignoreCase) Pattern.CASE_INSENSITIVE or Pattern.UNICODE_CASE else 0
        return try {
            Pattern.compile(p, base or Pattern.UNICODE_CHARACTER_CLASS)
        } catch (_: RuntimeException) { // IllegalArgument / UnsupportedOperation on some Android builds
            Pattern.compile(p, base)
        }
    }

    fun search(p: Pattern, s: String?): Boolean = p.matcher(s ?: "").find()
}

/** `models.normalize_key` */
fun normalizeKey(artist: String?, title: String?): String = "${normKeyPart(artist)}::${normKeyPart(title)}"

private fun normKeyPart(s: String?): String {
    val cf = Py.casefold((s ?: "").trim())
    val out = StringBuilder()
    var i = 0
    while (i < cf.length) {
        val cp = cf.codePointAt(i)
        when {
            Py.isAlnum(cp) || Py.isSpace(cp) -> out.appendCodePoint(cp)
            cp == '-'.code || cp == '_'.code || cp == '\''.code -> out.append(' ')
        }
        i += Character.charCount(cp)
    }
    return Py.collapseWs(out.toString())
}
