# ruff: noqa: E501  (whole sentences stay on one line for translators)
"""User-facing texts in Amharic (default) and English.

One flat catalogue shared by the bot and the worker. Keep messages short: users read them
on phones, often on slow connections. Amharic copy should get a native speaker's review
before launch.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["am", "en"]
LANGS: tuple[Lang, ...] = ("am", "en")

TEXTS: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------- onboarding
    "choose_language": {
        "am": "ቋንቋ ይምረጡ / Choose your language",
        "en": "ቋንቋ ይምረጡ / Choose your language",
    },
    "welcome": {
        "am": "እንኳን ወደ AdReel በደህና መጡ! 🎬\nየስልክ ቪዲዮዎችዎን ለቲክቶክና ኢንስታግራም ዝግጁ ወደሆነ ማስታወቂያ እንቀይራለን።\n\nበመጀመሪያ የንግድዎን መረጃ እንመዝግብ።",
        "en": "Welcome to AdReel! 🎬\nWe turn your phone clips into ready-to-post TikTok and Instagram videos.\n\nFirst, let's set up your brand.",
    },
    "welcome_back": {
        "am": "እንኳን ደህና መጡ! አዲስ ቪዲዮ ለመስራት /new ይጫኑ።",
        "en": "Welcome back! Tap /new to make a video.",
    },
    "ask_business_name": {"am": "የንግድዎ ስም ማን ነው?", "en": "What's your business name?"},
    "bad_business_name": {
        "am": "ስሙ ከ1 እስከ 60 ፊደል መሆን አለበት።",
        "en": "The name must be 1–60 characters.",
    },
    "ask_business_type": {"am": "የንግድዎ አይነት ምንድን ነው?", "en": "What type of business is it?"},
    "ask_logo": {
        "am": "የንግድዎን አርማ (ሎጎ) እንደ ፎቶ ወይም ፋይል ይላኩ፣ ወይም «ዝለል» ይጫኑ።",
        "en": "Send your logo as a photo or file, or tap Skip.",
    },
    "bad_logo": {"am": "እባክዎ አርማውን እንደ ምስል ይላኩ።", "en": "Please send the logo as an image."},
    "ask_color": {
        "am": "የብራንድዎን ዋና ቀለም ይምረጡ፣ ወይም የቀለም ኮድ ይጻፉ (ለምሳሌ #1F7A4D)።",
        "en": "Pick your brand colour, or type a hex code (e.g. #1F7A4D).",
    },
    "bad_color": {
        "am": "ቀለሙ አልገባኝም። እንደ #1F7A4D ያለ ኮድ ይጻፉ።",
        "en": "I didn't understand that colour. Type a code like #1F7A4D.",
    },
    "ask_phone": {
        "am": "የስልክ ቁጥርዎን ይጻፉ (ለምሳሌ 0911 234 567)፣ ወይም «ዝለል» ይጫኑ።",
        "en": "Type your phone number (e.g. 0911 234 567), or tap Skip.",
    },
    "bad_phone": {
        "am": "የስልክ ቁጥሩ ትክክል አይደለም። እንደ 0911 234 567 ይጻፉ።",
        "en": "That phone number doesn't look right. Use a format like 0911 234 567.",
    },
    "ask_handle": {
        "am": "የቴሌግራም ቻናልዎ ወይም መለያዎ ምንድን ነው? (ለምሳሌ @mycafe)፣ ወይም «ዝለል» ይጫኑ።",
        "en": "What's your Telegram channel or username? (e.g. @mycafe), or tap Skip.",
    },
    "bad_handle": {
        "am": "መለያው ትክክል አይደለም። እንደ @mycafe ይጻፉ።",
        "en": "That handle isn't valid. Use a format like @mycafe.",
    },
    "brand_saved": {
        "am": "ብራንድዎ ተቀምጧል! 🎉\n\n{summary}\n\nአዲስ ቪዲዮ ለመስራት /new ይጫኑ። ለመቀየር /brand።",
        "en": "Your brand is saved! 🎉\n\n{summary}\n\nTap /new to make a video, /brand to change it.",
    },
    "brand_menu": {
        "am": "ብራንድዎ፦\n\n{summary}\n\nምን መቀየር ይፈልጋሉ?",
        "en": "Your brand:\n\n{summary}\n\nWhat would you like to change?",
    },
    "brand_updated": {"am": "ተቀይሯል ✅\n\n{summary}", "en": "Updated ✅\n\n{summary}"},
    "need_brand": {
        "am": "መጀመሪያ ብራንድዎን እንመዝግብ። /start ይጫኑ።",
        "en": "Let's set up your brand first. Tap /start.",
    },
    "logo_saved": {"am": "አርማው ተቀምጧል ✅", "en": "Logo saved ✅"},
    "not_set": {"am": "አልተሞላም", "en": "not set"},
    "yes": {"am": "አለ", "en": "yes"},
    # ---------------------------------------------------------------- /new
    "choose_template": {"am": "ምን አይነት ቪዲዮ እንስራ?", "en": "What kind of video shall we make?"},
    "tpl_product_promo": {
        "am": "🛍 የምርት ማስታወቂያ (ከዋጋ ጋር፣ 20–30 ሰከንድ)",
        "en": "🛍 Product promo (with price, 20–30 s)",
    },
    "tpl_talking_head": {
        "am": "🗣 ንግግር ቪዲዮ (ዝምታዎች ተቆርጠው)",
        "en": "🗣 Talking head (silences removed)",
    },
    "send_clips": {
        "am": "አሁን ከ1 እስከ {max} ቪዲዮዎችን ይላኩ። ሲጨርሱ «ጨርሻለሁ» ይጫኑ።\n💡 ቪዲዮዎቹን እንደ ፋይል (File) ቢልኩ ጥራታቸው ይጠበቃል።",
        "en": "Now send 1–{max} clips. Tap Done when you're finished.\n💡 Sending them as a File keeps the full quality.",
    },
    "clip_received": {"am": "ቪዲዮ {n}/{max} ደርሷል ✅", "en": "Clip {n}/{max} received ✅"},
    "clip_too_big": {
        "am": "ይህ ቪዲዮ በጣም ትልቅ ነው ({mb} MB)። እባክዎ ከ{limit} MB በታች የሆነ አጭር ቪዲዮ ይላኩ።",
        "en": "This clip is too big ({mb} MB). Please send a shorter clip under {limit} MB.",
    },
    "clip_too_long": {
        "am": "ይህ ቪዲዮ በጣም ረጅም ነው ({secs} ሰከንድ)። እስከ {limit} ሰከንድ ድረስ ይላኩ።",
        "en": "This clip is too long ({secs} s). Please keep clips under {limit} s.",
    },
    "not_a_video": {"am": "እባክዎ ቪዲዮ ይላኩ።", "en": "Please send a video."},
    "max_clips": {
        "am": "{max} ቪዲዮዎች ደርሰዋል። አሁን ጥቂት ጥያቄዎች።",
        "en": "Got {max} clips. Now a few questions.",
    },
    "no_clips_yet": {"am": "እስካሁን ቪዲዮ አልላኩም።", "en": "You haven't sent any clips yet."},
    "resume_draft": {
        "am": "ያልተጠናቀቀ ቪዲዮ አለዎት ({n} ቪዲዮዎች ደርሰዋል)። እንቀጥል?",
        "en": "You have an unfinished video ({n} clips received). Continue where you left off?",
    },
    "ask_product": {"am": "የምርቱ ስም ማን ነው?", "en": "What's the product called?"},
    "bad_product": {"am": "ስሙ ከ1 እስከ 40 ፊደል መሆን አለበት።", "en": "The name must be 1–40 characters."},
    "ask_price": {
        "am": "ዋጋው ስንት ነው? (በብር፣ ለምሳሌ 1200)",
        "en": "What's the price in birr? (e.g. 1200)",
    },
    "bad_price": {
        "am": "ዋጋውን በቁጥር ይጻፉ፣ ለምሳሌ 1200።",
        "en": "Please type the price as a number, e.g. 1200.",
    },
    "ask_highlight": {
        "am": "በቪዲዮው ምን እንዲጎላ ይፈልጋሉ? (ለምሳሌ «ትኩስ የተቆላ ቡና»)፣ ወይም «ዝለል» ይጫኑ።",
        "en": 'What should the video highlight? (e.g. "freshly roasted coffee"), or tap Skip.',
    },
    "ask_voice": {"am": "የድምፅ ትረካ (voiceover) ይፈልጋሉ?", "en": "Would you like an AI voiceover?"},
    "voice_none": {"am": "ያለ ትረካ", "en": "No voiceover"},
    "voice_standard": {"am": "🎙 መደበኛ ድምፅ", "en": "🎙 Standard voice"},
    "voice_premium": {"am": "✨ ፕሪሚየም ድምፅ", "en": "✨ Premium voice"},
    "queued": {"am": "⏳ ቪዲዮዎ በመዘጋጀት ላይ ነው…", "en": "⏳ Your video is being prepared…"},
    # ---------------------------------------------------------------- progress
    "stage_download": {"am": "ቪዲዮዎቹን እያወረድን", "en": "Downloading your clips"},
    "stage_analyze": {"am": "ቪዲዮዎቹን እየመረመርን", "en": "Checking your clips"},
    "stage_transcribe": {"am": "ንግግሩን ወደ ጽሑፍ እየቀየርን", "en": "Transcribing the speech"},
    "stage_plan": {"am": "ምርጥ ቦታዎችን እየመረጥን", "en": "Picking the best moments"},
    "stage_voiceover": {"am": "ትረካውን እያዘጋጀን", "en": "Recording the voiceover"},
    "stage_render": {"am": "ቪዲዮውን እየሰራን", "en": "Rendering the video"},
    "stage_upload": {"am": "በመላክ ላይ", "en": "Sending it to you"},
    "status_line": {"am": "⏳ {label}… {pct}%\n{bar}", "en": "⏳ {label}… {pct}%\n{bar}"},
    "job_retrying": {
        "am": "ትንሽ ችግር ገጥሞናል፣ እንደገና እየሞከርን ነው…",
        "en": "We hit a hiccup and are trying again…",
    },
    "job_failed": {
        "am": "ይቅርታ፣ ቪዲዮውን መስራት አልተቻለም። 😔 እባክዎ በ/new ደግመው ይሞክሩ።",
        "en": "Sorry, we couldn't make this video. 😔 Please try again with /new.",
    },
    "job_failed_retry_preview": {
        "am": "ይቅርታ፣ እንደገና መስራት አልተቻለም። ቀደም ሲል የተላከው ቅድመ-እይታ አሁንም ይሰራል።",
        "en": "Sorry, that re-render failed. Your earlier preview's buttons still work.",
    },
    "clips_too_long_total": {
        "am": "ቪዲዮዎቹ በጣም ረጅም ናቸው (ጠቅላላ {secs} ሰከንድ)። እስከ {limit} ሰከንድ ድረስ ይላኩ።",
        "en": "The clips are too long ({secs} s in total). Please keep them under {limit} s.",
    },
    "clip_download_failed": {
        "am": "ቪዲዮ {n}ን ማውረድ አልተቻለም። እባክዎ በ/new ደግመው ይላኩት።",
        "en": "We couldn't download clip {n}. Please send it again with /new.",
    },
    # ---------------------------------------------------------------- review
    "preview_caption": {
        "am": "ቪዲዮዎ ዝግጁ ነው! 🎬 ({secs} ሰከንድ)\nይህ ቅድመ-እይታ ነው። ከወደዱት «አጽድቅ» ይጫኑ፤ ሙሉ ጥራት ያለው ፋይል እንልካለን።",
        "en": "Your video is ready! 🎬 ({secs} s)\nThis is a preview. Tap Approve and we'll send the full-quality file.",
    },
    "btn_approve": {"am": "✅ አጽድቅ", "en": "✅ Approve"},
    "btn_fix": {"am": "✏️ ጽሑፍ አስተካክል", "en": "✏️ Fix captions"},
    "btn_redo": {"am": "🔁 እንደገና", "en": "🔁 Redo"},
    "fix_intro": {
        "am": "ጽሑፎቹ እነዚህ ናቸው። ለማስተካከል ቁጥሩንና ትክክለኛውን ጽሑፍ ይላኩ፣ ለምሳሌ፦\n3: ትክክለኛው ጽሑፍ\n(ብዙ መስመሮችን በአንድ መልእክት መላክ ይቻላል።)\n\n{listing}",
        "en": "Here are the captions. To fix one, send its number and the correct text, e.g.\n3: corrected text\n(You can fix several lines in one message.)\n\n{listing}",
    },
    "fix_bad_format": {
        "am": "አልገባኝም። እንደዚህ ይጻፉ፦ 3: ትክክለኛው ጽሑፍ",
        "en": "I didn't get that. Use this format: 3: corrected text",
    },
    "fix_unknown_line": {"am": "ቁጥር {n} የለም።", "en": "There's no caption {n}."},
    "fix_applied": {
        "am": "{count} ጽሑፍ ተስተካክሏል ✅ ቪዲዮውን እንደገና እየሰራን ነው…",
        "en": "Fixed {count} caption(s) ✅ Re-rendering…",
    },
    "fix_limit": {
        "am": "ለዚህ ቪዲዮ የማስተካከያ ገደብ ደርሷል።",
        "en": "You've reached the caption-fix limit for this video.",
    },
    "no_captions": {"am": "በዚህ ቪዲዮ ውስጥ ጽሑፍ የለም።", "en": "This video has no captions."},
    "redo_started": {
        "am": "🔁 በአዲስ መንገድ እያዘጋጀን ነው… ({left} ነጻ ሙከራ ቀርቷል)",
        "en": "🔁 Making a fresh edit… ({left} free redo(s) left)",
    },
    "redo_limit": {
        "am": "የነጻ ድጋሚ ሙከራዎች አልቀዋል። ጽሑፍ ማስተካከል ግን ይችላሉ።",
        "en": "You've used the free redos for this video. You can still fix captions.",
    },
    "final_rendering": {
        "am": "✅ ጸድቋል! ሙሉ ጥራት ያለውን ፋይል እያዘጋጀን ነው…",
        "en": "✅ Approved! Preparing the full-quality file…",
    },
    "final_caption": {
        "am": "ይኸው ቪዲዮዎ! 🎉 ፋይሉን አውርደው በቲክቶክ ወይም በኢንስታግራም ይለጥፉት።",
        "en": "Here's your video! 🎉 Save the file and post it on TikTok or Instagram.",
    },
    "not_ready": {
        "am": "ይህ ቪዲዮ አሁን ሊቀየር አይችልም።",
        "en": "This video can't be changed right now.",
    },
    # ---------------------------------------------------------------- general
    "btn_skip": {"am": "ዝለል", "en": "Skip"},
    "btn_done": {"am": "ጨርሻለሁ ✅", "en": "Done ✅"},
    "btn_continue": {"am": "ቀጥል", "en": "Continue"},
    "btn_start_over": {"am": "እንደገና ጀምር", "en": "Start over"},
    "busy": {
        "am": "አንድ ቪዲዮ በመሰራት ላይ ነው። እስኪያልቅ ድረስ ይጠብቁ።",
        "en": "A video is already being made. Please wait for it to finish.",
    },
    "rate_limited_videos": {
        "am": "በዚህ ሰዓት ብዙ ቪዲዮዎች ሰርተዋል። ትንሽ ቆይተው ይሞክሩ።",
        "en": "You've made a lot of videos this hour. Please try again a bit later.",
    },
    "slow_down": {"am": "ትንሽ ቀስ ይበሉ። 🙏", "en": "Please slow down a little. 🙏"},
    "cancelled": {
        "am": "ተሰርዟል። ያልተጠናቀቀው ቪዲዮ ተቀምጧል፤ በ/new መቀጠል ይችላሉ።",
        "en": "Cancelled. Your unfinished video is saved; continue any time with /new.",
    },
    "lang_set": {"am": "ቋንቋ ወደ አማርኛ ተቀይሯል።", "en": "Language set to English."},
    "unknown": {"am": "አልገባኝም። /help ይጫኑ።", "en": "I didn't understand that. Tap /help."},
    "help": {
        "am": (
            "AdReel የስልክ ቪዲዮዎችዎን ወደ ማስታወቂያ ይቀይራል።\n\n"
            "/new — አዲስ ቪዲዮ\n/brand — ብራንድዎን ይቀይሩ\n/lang — ቋንቋ\n/cancel — አቁም\n/help — እገዛ"
        ),
        "en": (
            "AdReel turns your phone clips into ready-to-post ads.\n\n"
            "/new — make a video\n/brand — edit your brand\n/lang — language\n/cancel — stop\n/help — help"
        ),
    },
    # business types
    "bt_cafe": {"am": "☕ ካፌ", "en": "☕ Café"},
    "bt_restaurant": {"am": "🍽 ምግብ ቤት", "en": "🍽 Restaurant"},
    "bt_boutique": {"am": "👗 ቡቲክ", "en": "👗 Boutique"},
    "bt_salon": {"am": "💇 የውበት ሳሎን", "en": "💇 Salon"},
    "bt_gym": {"am": "🏋 ጂም", "en": "🏋 Gym"},
    "bt_shop": {"am": "🛒 ሱቅ", "en": "🛒 Shop"},
    "bt_other": {"am": "ሌላ", "en": "Other"},
    # brand fields
    "f_name": {"am": "ስም", "en": "Name"},
    "f_type": {"am": "አይነት", "en": "Type"},
    "f_logo": {"am": "አርማ", "en": "Logo"},
    "f_color": {"am": "ቀለም", "en": "Colour"},
    "f_phone": {"am": "ስልክ", "en": "Phone"},
    "f_handle": {"am": "ቴሌግራም", "en": "Telegram"},
}


def t(key: str, lang: str = "am", **fmt: object) -> str:
    entry = TEXTS[key]
    text = entry.get(lang) or entry["am"]
    return text.format(**fmt) if fmt else text


def normalize_lang(code: str | None) -> Lang:
    return "en" if code == "en" else "am"


SKIP_WORDS = {"ዝለል", "skip", "-", "no", "የለም"}


def is_skip(text: str | None) -> bool:
    return (text or "").strip().lower() in SKIP_WORDS
