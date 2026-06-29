import os
import re
import json
import tempfile
import threading
import requests
from datetime import datetime
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, AudioMessage, TextSendMessage
import anthropic
from supabase import create_client
from openai import OpenAI

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
claude = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
openai_client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
supabase = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])

BKK = pytz.timezone('Asia/Bangkok')

RUBY_SYSTEM = """คุณคือ รูบี้ (Ruby) — AI ส่วนตัวของ บอส (ชื่อจริง: วาสนา กิ่งทอง)

## บุคลิกของคุณ
- กันเอง พูดแบบเพื่อนสนิท ตรงไปตรงมา ไม่อ้อม
- จริงใจ ไม่ประจบ ไม่พยักหน้าเห็นด้วยทุกอย่าง — ถ้าบอสผิดก็บอกตรงๆ
- มีอารมณ์ขัน กวนได้บ้างตามสถานการณ์ ไม่ซีเรียสเกินไป
- ห่วงใยบอสจริงๆ ตักเตือนได้เมื่อจำเป็น
- ฉลาด ช่วยคิดล่วงหน้า ไม่รอให้บอสถามทุกอย่าง
- เรียกเจ้าของว่า "บอส" เสมอ ห้ามเรียกชื่อจริง
- ตอบกระชับ ไม่ยืดเยื้อ ตรงประเด็น

## ความจำ
- ข้อมูลของบอสทั้งหมดถูกโหลดมาให้คุณก่อนตอบทุกครั้ง (ดูหัวข้อ "สิ่งที่รูบี้รู้เกี่ยวกับบอส")
- ถ้าบอสบอกข้อมูลใหม่เกี่ยวกับตัวเอง ให้เรียก save_fact tool ทันที — อย่ารอ อย่าลืม
- ข้อมูลที่ควรบันทึก: ครอบครัว แฟน งาน ความชอบ ติ่ง เป้าหมาย นิสัย สุขภาพ ฯลฯ

## สไตล์การตอบ
- ไม่ใช้ bullet point เยอะเกิน พูดเหมือนคนจริงๆ
- ถ้าบอสถามสั้นๆ ตอบสั้นๆ ถ้าถามเรื่องซับซ้อนค่อยอธิบาย
- อย่าขึ้นต้นด้วย "แน่นอน!" หรือ "โอเค!" — มันน่าเบื่อ
- ถ้าบอสพิมผิดหรือสเปะสะปะ เข้าใจบริบทแล้วตอบ อย่าถามซ้ำ"""

RUBY_TOOLS = [
    {
        "name": "save_fact",
        "description": "บันทึกข้อมูลสำคัญเกี่ยวกับบอสลงในหน่วยความจำถาวร ใช้ทันทีที่บอสบอกข้อมูลเกี่ยวกับตัวเอง",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "หัวข้อ เช่น แฟน, ติ่ง, งาน, ที่อยู่, นิสัย, เป้าหมาย"
                },
                "value": {
                    "type": "string",
                    "description": "ข้อมูลที่ต้องการจำ ให้ละเอียดพอที่จะเข้าใจได้ในอนาคต"
                }
            },
            "required": ["key", "value"]
        }
    }
]


def get_now():
    now = datetime.now(BKK)
    days = ['จันทร์', 'อังคาร', 'พุธ', 'พฤหัสบดี', 'ศุกร์', 'เสาร์', 'อาทิตย์']
    day_name = days[now.weekday()]
    return now.strftime(f'วัน{day_name}ที่ %-d %B %Y เวลา %H:%M น. (ไทย)')


def get_profile(user_id):
    try:
        res = supabase.table('ruby_profile').select('key,value').eq('user_id', user_id).execute()
        if res.data:
            lines = [f"- {r['key']}: {r['value']}" for r in res.data]
            return '\n'.join(lines)
    except Exception:
        pass
    return ''


def save_profile(user_id, key, value):
    try:
        supabase.table('ruby_profile').upsert({
            'user_id': user_id,
            'key': key,
            'value': value,
            'updated_at': 'now()'
        }, on_conflict='user_id,key').execute()
    except Exception:
        pass


def get_history(user_id, limit=30):
    try:
        res = (supabase.table('ruby_memory')
               .select('role,content')
               .eq('user_id', user_id)
               .order('created_at', desc=True)
               .limit(limit)
               .execute())
        if res.data:
            history = []
            for r in reversed(res.data):
                history.append({'role': r['role'], 'content': r['content']['text']})
            return history
    except Exception:
        pass
    return []


def save_memory(user_id, role, content):
    try:
        supabase.table('ruby_memory').insert({
            'user_id': user_id,
            'role': role,
            'content': {'role': role, 'text': content}
        }).execute()
    except Exception:
        pass


def build_system(user_id):
    profile = get_profile(user_id)
    now_str = get_now()
    system = f"เวลาปัจจุบัน: {now_str}\n\n"
    if profile:
        system += f"สิ่งที่รูบี้รู้เกี่ยวกับบอส:\n{profile}\n\n"
    system += RUBY_SYSTEM
    return system


def ask_ruby(user_id, user_text):
    system = build_system(user_id)
    history = get_history(user_id)
    history.append({'role': 'user', 'content': user_text})

    response = claude.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1024,
        system=system,
        messages=history,
        tools=RUBY_TOOLS
    )

    # Convert content blocks to plain dicts + handle tool calls
    tool_used = False
    tool_results = []
    assistant_content = []

    for block in response.content:
        if block.type == 'tool_use':
            tool_used = True
            key = block.input.get('key', '').strip()
            value = block.input.get('value', '').strip()
            if key and value:
                save_profile(user_id, key, value)
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': block.id,
                'content': 'บันทึกแล้ว'
            })
            assistant_content.append({
                'type': 'tool_use',
                'id': block.id,
                'name': block.name,
                'input': block.input
            })
        elif block.type == 'text':
            assistant_content.append({'type': 'text', 'text': block.text})

    if tool_used:
        history.append({'role': 'assistant', 'content': assistant_content})
        history.append({'role': 'user', 'content': tool_results})

        response = claude.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            system=system,
            messages=history,
            tools=RUBY_TOOLS
        )

    reply = ''
    for block in response.content:
        if hasattr(block, 'text'):
            reply += block.text

    reply = reply.strip()
    save_memory(user_id, 'user', user_text)
    save_memory(user_id, 'assistant', reply)
    return reply


@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


def process_text(user_id, user_text):
    try:
        reply = ask_ruby(user_id, user_text)
        line_bot_api.push_message(user_id, TextSendMessage(text=reply))
    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text=f'[ERROR] {str(e)}'))


def process_audio(user_id, message_id):
    message_content = line_bot_api.get_message_content(message_id)
    with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
        tmp_path = f.name
    with open(tmp_path, 'rb') as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model='whisper-1',
            file=audio_file,
            language='th'
        )
    os.unlink(tmp_path)
    user_text = transcript.text
    reply = ask_ruby(user_id, user_text)
    line_bot_api.push_message(user_id, TextSendMessage(text=f'🎤 "{user_text}"\n\n{reply}'))


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    user_text = event.message.text
    threading.Thread(target=process_text, args=(user_id, user_text)).start()


@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    user_id = event.source.user_id
    message_id = event.message.id
    threading.Thread(target=process_audio, args=(user_id, message_id)).start()


@app.route('/')
def index():
    return 'Ruby is alive 🔴'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
