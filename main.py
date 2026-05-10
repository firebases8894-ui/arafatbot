import telebot
import requests
import re
import os
import json
import threading

BOT_TOKEN = "8766011800:AAGRDii1rgQkJvzqztIIlv56sXknkadFzFM"
OPENROUTER_KEY = "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"

bot = telebot.TeleBot(BOT_TOKEN)

# প্রতি ইউজারের pending ফাইল সংরক্ষণ
user_pending_files = {}  # { user_id: [(filename, content), ...] }

# Model list — দ্রুত থেকে ধীরে ক্রমানুসারে (fallback)
MODELS = [
    "mistralai/mistral-7b-instruct:free",
    "google/gemma-3-12b-it:free",
    "z-ai/glm-4.5-air:free",
]

TIMEOUT = 60  # সেকেন্ড

# =========================
# Typing Loop
# =========================
def keep_typing(bot, chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except:
            pass
        stop_event.wait(timeout=4)

# =========================
# Code Block Detector
# =========================
def extract_code(text):
    pattern = r"```(\w+)?\n([\s\S]*?)```"
    return re.findall(pattern, text)

# =========================
# Streaming AI Call (দ্রুত response)
# =========================
def call_ai_streaming(messages, model):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": model,
        "messages": messages,
        "stream": True
    }

    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=data,
        stream=True,
        timeout=TIMEOUT
    )

    full_text = ""
    for line in r.iter_lines():
        if line:
            line = line.decode("utf-8")
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                    delta = obj["choices"][0]["delta"].get("content", "")
                    full_text += delta
                except:
                    continue

    return full_text.strip()

# =========================
# Model Fallback সহ AI Call
# =========================
def call_ai_with_fallback(messages):
    last_error = None
    for model in MODELS:
        try:
            result = call_ai_streaming(messages, model)
            if result:
                return result
        except requests.exceptions.Timeout:
            last_error = f"Model `{model}` timeout হয়েছে, পরের model চেষ্টা করছি..."
            continue
        except Exception as e:
            last_error = str(e)
            continue

    raise Exception(f"সব model ব্যর্থ হয়েছে। শেষ error: {last_error}")

# =========================
# AI দিয়ে ফাইল আপডেট
# =========================
def process_files_with_ai(files, prompt):
    files_content = ""
    for filename, content in files:
        files_content += f"\n\n=== FILE: {filename} ===\n{content}\n=== END: {filename} ==="

    full_prompt = f"""You are a code editor assistant. Update the following file(s) based on the user's instruction.

USER INSTRUCTION: {prompt}

{files_content}

Return ONLY the updated file(s) in this exact format (no explanation):
=== UPDATED: filename ===
[full updated content]
=== END UPDATED: filename ==="""

    messages = [{"role": "user", "content": full_prompt}]
    return call_ai_with_fallback(messages)

# =========================
# AI Response থেকে ফাইল বের করা
# =========================
def parse_updated_files(response, original_files):
    updated = {}

    pattern = r"=== UPDATED: (.+?) ===\n([\s\S]*?)=== END UPDATED: .+? ==="
    matches = re.findall(pattern, response)

    for filename, content in matches:
        updated[filename.strip()] = content.strip()

    # Fallback: single file + code block
    if not updated and len(original_files) == 1:
        code_blocks = extract_code(response)
        if code_blocks:
            _, code = code_blocks[0]
            updated[original_files[0][0]] = code.strip()

    return updated

# =========================
# আপডেট ফাইল পাঠানো
# =========================
def send_updated_files(chat_id, updated_files):
    if not updated_files:
        bot.send_message(chat_id, "⚠️ ফাইল আপডেট করা যায়নি, আবার চেষ্টা করুন।")
        return

    for fname, fcontent in updated_files.items():
        with open(fname, "w", encoding="utf-8") as f:
            f.write(fcontent)
        with open(fname, "rb") as f:
            bot.send_document(chat_id, f, caption=f"✅ আপডেট হয়েছে: {fname}")
        os.remove(fname)

# =========================
# Document Handler (ফাইল আপলোড)
# =========================
@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id

    # ফাইল ডাউনলোড
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename = message.document.file_name
        content = downloaded.decode("utf-8")
    except UnicodeDecodeError:
        bot.send_message(message.chat.id, f"❌ `{message.document.file_name}` বাইনারি ফাইল। শুধু টেক্সট/কোড ফাইল পাঠান।")
        return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ ফাইল পড়তে সমস্যা:\n{e}")
        return

    caption = message.caption

    # ── Mode 1: ফাইল + Caption → সাথে সাথে প্রসেস ──
    if caption:
        stop_event = threading.Event()
        threading.Thread(target=keep_typing, args=(bot, message.chat.id, stop_event), daemon=True).start()

        try:
            response = process_files_with_ai([(filename, content)], caption)
            updated_files = parse_updated_files(response, [(filename, content)])
            stop_event.set()
            send_updated_files(message.chat.id, updated_files)

        except Exception as e:
            stop_event.set()
            bot.send_message(message.chat.id, f"❌ Error:\n{e}")

    # ── Mode 2: শুধু ফাইল → পরে prompt দিতে বলো ──
    else:
        if user_id not in user_pending_files:
            user_pending_files[user_id] = []

        user_pending_files[user_id].append((filename, content))
        count = len(user_pending_files[user_id])
        files_list = "\n".join([f"  📄 {f[0]}" for f in user_pending_files[user_id]])

        bot.send_message(
            message.chat.id,
            f"✅ *{count}টি ফাইল* সেভ হয়েছে:\n{files_list}\n\n"
            f"আরো ফাইল পাঠান অথবা কী করতে চান তা লিখুন।\n"
            f"_(বাতিল করতে /clear লিখুন)_",
            parse_mode="Markdown"
        )

# =========================
# /clear Command
# =========================
@bot.message_handler(commands=['clear'])
def clear_files(message):
    user_id = message.from_user.id
    user_pending_files.pop(user_id, None)
    bot.send_message(message.chat.id, "🗑️ সব pending ফাইল মুছে গেছে।")

# =========================
# /start Command
# =========================
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "👋 *স্বাগতম!*\n\n"
        "আমি একটি AI কোড এডিটর বট।\n\n"
        "📌 *ব্যবহার পদ্ধতি:*\n"
        "• কোড ফাইল পাঠান + caption এ instruction দিন\n"
        "• বা ফাইল পাঠান, তারপর text এ বলুন কী করতে হবে\n"
        "• /clear — pending ফাইল মুছতে\n\n"
        "⚡ Streaming চালু আছে — দ্রুত response পাবেন!",
        parse_mode="Markdown"
    )

# =========================
# Text Handler
# =========================
@bot.message_handler(func=lambda m: True)
def chat(message):
    user_id = message.from_user.id
    user_text = message.text
    pending_files = user_pending_files.get(user_id, [])

    stop_event = threading.Event()
    threading.Thread(target=keep_typing, args=(bot, message.chat.id, stop_event), daemon=True).start()

    try:
        # ── Pending ফাইল থাকলে → ফাইল আপডেট মোড ──
        if pending_files:
            response = process_files_with_ai(pending_files, user_text)
            updated_files = parse_updated_files(response, pending_files)
            stop_event.set()

            user_pending_files.pop(user_id, None)  # clear করো
            send_updated_files(message.chat.id, updated_files)

        # ── Normal Chat মোড ──
        else:
            messages = [{"role": "user", "content": user_text}]
            reply = call_ai_with_fallback(messages)
            stop_event.set()

            code_blocks = extract_code(reply)

            if code_blocks:
                ext_map = {
                    "python": "py", "html": "html", "javascript": "js",
                    "css": "css", "php": "php", "json": "json",
                    "bash": "sh", "shell": "sh", "sql": "sql",
                    "java": "java", "cpp": "cpp", "c": "c"
                }

                for index, (lang, code) in enumerate(code_blocks):
                    ext = ext_map.get(lang.lower(), "txt") if lang else "txt"
                    filename = f"code_{index}.{ext}"

                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(code)
                    with open(filename, "rb") as f:
                        bot.send_document(message.chat.id, f, caption=f"📁 {filename}")
                    os.remove(filename)

                # Code block ছাড়া বাকি text থাকলে পাঠাও
                plain_text = re.sub(r"```[\s\S]*?```", "", reply).strip()
                if plain_text:
                    bot.send_message(message.chat.id, plain_text[:4000])
            else:
                if len(reply) > 4000:
                    for i in range(0, len(reply), 4000):
                        bot.send_message(message.chat.id, reply[i:i+4000])
                else:
                    bot.send_message(message.chat.id, reply)

    except Exception as e:
        stop_event.set()
        bot.send_message(message.chat.id, f"❌ Error:\n{e}")

    finally:
        stop_event.set()

print("✅ Bot Running... (Streaming চালু)")
bot.infinity_polling()
