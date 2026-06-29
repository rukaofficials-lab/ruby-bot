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


EXTRACT_SYSTEM = """คุณคือระบบ extract ข้อมูลจากบทสนทนา ตอบ JSON เท่านั้น ห้ามมีข้อความอื่น

ดูบทสนทนาแล้วหา:
1. ข้อมูลส่วนตัวของบอส (ชื่อ แฟน ครอบครัว งาน ความชอบ ติ่ง เป้าหมาย นิสัย สุขภาพ)
2. การขอให้เตือน/แจ้งเตือนตามเวลา

ตอบในรูปแบบ:
{
  "facts": [{"key": "หัวข้อ", "value": "ข้อมูล"}],
  "reminders": [{"time_th": "เวลาภาษาไทย เช่น วันนี้ 4 ทุ่ม", "message": "ข้อความเตือน"}]
}

ถ้าไม่มีก็ใส่ array ว่าง [] ตอบ JSON เท่านั้น"""


def parse_thai_time(time_th, now):
    """ให้ Claude แปลงเวลาไทยเป็น ISO timestamp"""
    try:
        resp = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=64,
            system='แปลงเวลาไทยเป็น ISO 8601 timezone Asia/Bangkok ตอบแค่ตัวเลข เช่น 2026-06-29T22:00:00+07:00 ห้ามมีข้อความอื่น',
            messages=[{'role': 'user', 'content': f'ตอนนี้คือ {now.strftime("%Y-%m-%dT%H:%M:%S+07:00")} เวลาที่ต้องการ: {time_th}'}]
        )
        ts_str = resp.content[0].text.strip()
        from datetime import timezone, timedelta
        TZ7 = timezone(timedelta(hours=7))
        from dateutil.parser import parse as dtparse
        return dtparse(ts_str).astimezone(TZ7)
    except Exception:
        return None


def save_reminder(user_id, remind_at, message):
    try:
        supabase.table('ruby_reminders').insert({
            'user_id': user_id,
            'hour': remind_at.hour,
            'minute': remind_at.minute,
            'message': message,
            'remind_at': remind_at.isoformat(),
            'enabled': True,
            'fired': False
        }).execute()
    except Exception:
        pass


def extract_and_save(user_id, user_text, assistant_reply):
    try:
        now = datetime.now(BKK)
        resp = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            system=EXTRACT_SYSTEM,
            messages=[{'role': 'user', 'content': f'ตอนนี้: {now.strftime("%Y-%m-%d %H:%M")}\nบอส: {user_text}\nรูบี้: {assistant_reply}'}]
        )
        data = json.loads(resp.content[0].text.strip())
        for f in data.get('facts', []):
            if f.get('key') and f.get('value'):
                save_profile(user_id, f['key'], f['value'])
        for r in data.get('reminders', []):
            if r.get('time_th') and r.get('message'):
                remind_at = parse_thai_time(r['time_th'], now)
                if remind_at:
                    save_reminder(user_id, remind_at, r['message'])
    except Exception:
        pass


def ask_ruby(user_id, user_text):
    system = build_system(user_id)
    history = get_history(user_id)
    history.append({'role': 'user', 'content': user_text})

    response = claude.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1024,
        system=system,
        messages=history
    )

    reply = response.content[0].text.strip()
    save_memory(user_id, 'user', user_text)
    save_memory(user_id, 'assistant', reply)

    # Extract facts in background (non-blocking)
    threading.Thread(target=extract_and_save, args=(user_id, user_text, reply)).start()

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


@app.route('/debug/<user_id>')
def debug(user_id):
    try:
        res = supabase.table('ruby_profile').select('key,value').eq('user_id', user_id).execute()
        return {'profile': res.data, 'count': len(res.data)}
    except Exception as e:
        return {'error': str(e)}


@app.route('/cron')
def cron():
    secret = os.environ.get('CRON_SECRET', '')
    if request.args.get('secret') != secret:
        abort(403)
    now = datetime.now(BKK)
    h, m = now.hour, now.minute
    try:
        sent = 0
        # Recurring reminders (hour + minute, no remind_at)
        res = (supabase.table('ruby_reminders')
               .select('user_id,message')
               .eq('hour', h).eq('minute', m)
               .eq('enabled', True)
               .is_('remind_at', 'null')
               .execute())
        for row in res.data:
            line_bot_api.push_message(row['user_id'], TextSendMessage(text=row['message']))
            sent += 1
        # One-time reminders — find those within current minute window
        now_iso = now.strftime('%Y-%m-%dT%H:%M')
        res2 = (supabase.table('ruby_reminders')
                .select('id,user_id,message')
                .eq('enabled', True)
                .eq('fired', False)
                .like('remind_at', f'{now_iso}%')
                .execute())
        for row in res2.data:
            line_bot_api.push_message(row['user_id'], TextSendMessage(text=row['message']))
            supabase.table('ruby_reminders').update({'fired': True}).eq('id', row['id']).execute()
            sent += 1
        return {'sent': sent, 'time': f'{h:02d}:{m:02d}'}
    except Exception as e:
        return {'error': str(e)}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
