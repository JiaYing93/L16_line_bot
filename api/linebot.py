from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    TemplateSendMessage, ButtonsTemplate, MessageAction, FlexSendMessage,
    ConfirmTemplate, ImageCarouselTemplate, ImageCarouselColumn,
    BubbleContainer, CarouselContainer, BoxComponent, TextComponent, ButtonComponent, URIAction,CarouselTemplate, CarouselColumn
)
from transitions.extensions import GraphMachine
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import tempfile
import sys
import logging
import re
import schedule
import time
from threading import Thread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

credentials = Credentials.from_service_account_info(
    json.loads(GOOGLE_SHEET_JSON),
    scopes=scope
)
gc = gspread.authorize(credentials)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
line_handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

SPREADSHEET_KEY = os.getenv("GOOGLE_SPREADSHEET_KEY")
logger.info(f"[DEBUG] 當前使用的 SPREADSHEET_KEY: {SPREADSHEET_KEY}")
BOOKING_OPTIONS_SHEETS = {
    '預約團體課程': '課程資料',
    '預約私人教練': '教練資料',
    '場地租借': '場地資料'
}
BOOKING_COLUMN_MAPPING = {
    '課程資料': '課程名稱',
    '教練資料': '專長',
    '場地資料': '名稱'
}
user_states = {}

class BookingFSM:
    def __init__(self, user_id, states, transitions, initial):
        # 您的初始化程式碼
        pass
def load_booking_options():
    global booking_options
    booking_options = {"categories": {}}
    try:
        client = get_gspread_client()
        for category, sheet_name in BOOKING_OPTIONS_SHEETS.items():
            column_name = BOOKING_COLUMN_MAPPING.get(sheet_name, "項目")
            logger.info(f"嘗試載入 {category} 的預約選項，工作表：{sheet_name}，欄位：{column_name}")

            try:
                sheet = client.open_by_key(SPREADSHEET_KEY).worksheet(sheet_name)
                records = sheet.get_all_records()

                if category == "預約私人教練":
                    # 🧠 私人教練特殊格式（需要兩欄：專長 和 教練姓名）
                    specialty_col = "專長"
                    coach_col = "姓名"
                    specialty_map = {}
                    for row in records:
                        spec = row.get(specialty_col)
                        coach = row.get(coach_col)
                        if spec and coach:
                            specialty_map.setdefault(spec, []).append(coach)
                    booking_options["categories"][category] = {"專長": specialty_map}
                else:
                    # ✅ 其他類別（團體課程/場地租借）
                    items = [row.get(column_name) for row in records if row.get(column_name)]
                    booking_options["categories"][category] = {"items": items}

                logger.info(f"✅ {category} 選項載入成功")

            except gspread.exceptions.WorksheetNotFound:
                logger.error(f"❌ 找不到工作表：{sheet_name}，跳過 {category}")
            except Exception as e:
                logger.error(f"❌ 載入 {category} 失敗：{e}", exc_info=True)

        logger.info("[DEBUG] booking_options 結構如下：")
        for category, content in booking_options["categories"].items():
            logger.info(f" - {category}：{content}")

    except Exception as e:
        logger.critical(f"❌ 預約資料整體載入失敗：{e}", exc_info=True)
        booking_options = {"categories": {}}

def process_booking(event, booking_category, booking_service, booking_date, booking_time, user_id, member_name):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_KEY).worksheet("預約選項")
        booking_data = [user_id, member_name, booking_category, booking_service, booking_date, booking_time]
        sheet.append_row(booking_data)
        line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 您的 {booking_category} - {booking_service} 預約已成功記錄！"))
    except Exception as e:
        logger.error(f"儲存預約資料到 Google Sheets 失敗：{e}", exc_info=True)
        line_bot_api.push_message(user_id, TextSendMessage(text="⚠ 儲存預約資料時發生錯誤，請稍後再試。"))

class BookingFSM(GraphMachine):
    def __init__(self, user_id, **configs):
        self.user_id = user_id
        self.member_name = ""
        super().__init__(**configs)
        self.reset_booking_data()

    def reset_booking_data(self):
        self.booking_category = None
        self.booking_service = None
        self.booking_date = None
        self.booking_time = None

    def ask_category(self, event):
        global booking_options
        categories = list(booking_options["categories"].keys())
        logger.info(f"ask_category 函數被呼叫，目前 booking_options: {booking_options}")  # 新增日誌
        if not categories:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前沒有可預約的類別，請稍後再試。"))
            self.go_back()
            return  # 確保在沒有類別時，函數在這裡結束
        else:
            buttons = [MessageAction(label=cat, text=cat) for cat in categories]
            template = TemplateSendMessage(
                alt_text="請選擇預約類別",
                template=ButtonsTemplate(
                    title="預約選項",
                    text="您想要預約什麼？",
                    actions=buttons
                )
            )
            line_bot_api.reply_message(event.reply_token, template)

    def process_category(self, event):
        self.booking_category = event.message.text
        logger.info(f"[FSM] 使用者選擇的類別：{self.booking_category}")

        services = booking_options["categories"].get(self.booking_category)
        logger.info(f"[FSM] 該類別對應服務選項：{services}")

        if not services:
            logger.warning(f"[FSM] 找不到任何服務選項 for 類別：{self.booking_category}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"❌ {self.booking_category} 目前沒有任何可預約項目，請稍後再試。")
            )
            self.go_back()
            return

    # ✅ 處理私人教練（雙層結構）
        if self.booking_category == "預約私人教練":
            specialties = list(services["專長"].keys())
            self.temp_data = {"專長列表": specialties}  # 暫存

        # 少於 4 項用 ButtonsTemplate
            if len(specialties) <= 4:
                buttons = [MessageAction(label=spec, text=spec) for spec in specialties]
                template = TemplateSendMessage(
                    alt_text="請選擇教練專長",
                    template=ButtonsTemplate(
                        title="選擇教練專長",
                        text="請選擇您想要預約的教練專長：",
                        actions=buttons
                    )
                )
                line_bot_api.reply_message(event.reply_token, template)
            else:
                self.ask_service(event, specialties, prompt="請選擇教練專長")
            self.state = "service_selection"  # 等待使用者選擇專長
            return

    # ✅ 處理單層結構（場地租借、團體課程）
        items = services.get("items", [])
        if not items:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 沒有可用的選項，請稍後再試。"))
            self.go_back()
            return

        if len(items) <= 4:
            buttons = [MessageAction(label=service, text=service) for service in items]
            template = TemplateSendMessage(
                alt_text="請選擇預約項目",
                template=ButtonsTemplate(
                    title=f"{self.booking_category} 預約",
                    text="您想預約哪個項目？",
                    actions=buttons
                )
            )
            line_bot_api.reply_message(event.reply_token, template)
        else:
            self.ask_service(event, items)

        self.next_state()

    def is_group_course_category(self, event):
        return event.message.text == "預約團體課程"

    def is_personal_coach_category(self, event):
        return event.message.text == "預約私人教練"

    def is_venue_rent_category(self, event):
        return event.message.text == "場地租借"
        
    def process_service(self, event):
        self.booking_service = event.message.text
        logger.info(f"[FSM] 使用者選擇項目：{self.booking_service}")

        if (
            self.booking_category
            and self.booking_category in booking_options["categories"]
            and self.booking_service in booking_options["categories"][self.booking_category]
        ):
            self.ask_date(event)
            self.next_state()
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"抱歉，'{self.booking_category}' 類別下沒有 '{self.booking_service}' 這個項目，請重新選擇。")
            )
            self.go_back()

    def ask_service(self, event, category):
        sheet_mapping = {
            '團體課程': 'GroupCourses',
            '私人教練': 'PrivateCoach',
            '場地租借': 'VenueRental'
        }
        sheet_name = sheet_mapping.get(category)
        if not sheet_name:
            self.line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='無效的類別，請重新選擇。')
            )
            return

        worksheet = self.spreadsheet.worksheet(sheet_name)
        services = worksheet.col_values(1)[1:]  # 跳過標題列

        if not services:
            self.line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text='找不到服務選項，請稍後再試。')
            )
            return

        # 依每4個服務為一組建立CarouselColumn（最多10個bubble）
        columns = []
        for i in range(0, min(len(services), 40), 4):  # LINE 最多支援40個選項
            actions = [
                MessageAction(label=service[:20], text=service)
                for service in services[i:i+4]
            ]
            column = CarouselColumn(
                thumbnail_image_url="https://i.imgur.com/NWz9V2O.png",  # 可換成你自己預設圖片
                title=f"{category} 選項",
                text="請選擇服務項目",
                actions=actions
            )
            columns.append(column)

        template_message = TemplateSendMessage(
            alt_text=f"{category} 服務選擇",
            template=CarouselTemplate(columns=columns)
        )

        self.line_bot_api.reply_message(event.reply_token, template_message)
    def select_service(self, event):
        user_input = event.message.text

    # ✅ 特別處理：預約私人教練（兩層選擇）
        if self.booking_category == "預約私人教練":
            services = booking_options["categories"][self.booking_category]["專長"]

        # 如果還沒選擇專長 → 那這次是選擇「專長」
            if not hasattr(self, 'coach_specialty'):
                if user_input not in services:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無效的專長選項，請重新選擇。"))
                    return
                self.coach_specialty = user_input  # 儲存專長
                coach_list = services[user_input]
                self.temp_data["教練列表"] = coach_list

            # 顯示教練名單
                if len(coach_list) <= 4:
                    buttons = [MessageAction(label=coach, text=coach) for coach in coach_list]
                    template = TemplateSendMessage(
                        alt_text="請選擇教練",
                        template=ButtonsTemplate(
                            title=f"{user_input} 專長的教練",
                            text="請選擇教練姓名：",
                            actions=buttons
                        )
                    )
                    line_bot_api.reply_message(event.reply_token, template)
                else:
                    self.ask_service(event, coach_list, prompt="請選擇教練姓名")

                return  # 還沒跳到下一步（等選完教練）

        # ✅ 已經選過專長 → 現在是選教練
            elif user_input in self.temp_data.get("教練列表", []):
                self.selected_service = user_input  # 選定的教練姓名
                self.next_state()
                self.ask_date(event)
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無效的教練姓名，請重新選擇。"))
                return

        else:
        # ✅ 一般類型（場地租借、團體課程）
            services = booking_options["categories"][self.booking_category].get("items", [])
            if user_input in services:
                self.selected_service = user_input
                self.next_state()
                self.ask_date(event)
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無效的預約項目，請重新選擇。"))

    def ask_date(self, event):
        if self.booking_category == "團體課程":
            message = "📅 請輸入欲上課的日期（格式：YYYY-MM-DD），請確認是否落在該課程的開課與結束日期之間。"
        else:
            message = "📅 請輸入預約日期（格式：YYYY-MM-DD）："
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=message))

    def enter_date(self, event):
        self.booking_date = event.message.text.strip()
        logger.info(f"[FSM] 使用者輸入日期：{self.booking_date}")
        self.ask_time(event)
        self.next_state()

    def ask_time(self, event):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入預約時間 (HH:MM)。"))

    def enter_time(self, event):
        user_input = event.message.text.strip()
        try:
            booking_time = datetime.strptime(user_input, "%H:%M").time()
            booking_datetime = datetime.combine(self.booking_date, booking_time)

            logger.info(f"[FSM] 使用者輸入時間：{booking_datetime}")

        # 檢查是否早於現在時間
            now = datetime.now()
            if booking_datetime < now:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="❌ 無法預約過去的時間，請重新輸入。")
                )
                return

        # 取得對應試算表與欄位
            sheet_name = BOOKING_SHEET_MAPPING.get(self.booking_category)
            if not sheet_name:
                raise ValueError("無法對應的工作表")

            column_name = "時間"
            date_column = "日期"
            target_column = None

        # 團體課程不做衝突檢查
            if self.booking_category == "團體課程":
                self.booking_time = booking_time
                self.next_state()
                return
            elif self.booking_category == "私人教練":
                target_column = "教練姓名"
                target_value = self.selected_service
            elif self.booking_category == "場地租借":
                target_column = "場地"
                target_value = self.selected_service
            else:
                raise ValueError("未知的預約類別")

        # 檢查衝突：讀取現有預約資料
            client = get_gspread_client()
            sheet = client.open_by_key(SPREADSHEET_KEY).worksheet(sheet_name)
            records = sheet.get_all_records()

            conflict_found = False
            for row in records:
                if (row.get(date_column) == self.booking_date.strftime("%Y-%m-%d") and
                    row.get(target_column) == target_value and
                    row.get(column_name)):

                    existing_time = datetime.strptime(row[column_name], "%H:%M").time()
                    existing_dt = datetime.combine(self.booking_date, existing_time)

                # 若時間差小於 2 小時，視為衝突
                    if abs((booking_datetime - existing_dt).total_seconds()) < 2 * 3600:
                        conflict_found = True
                        break

            if conflict_found:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="❌ 此時間已有人預約，請選擇距離他人至少 2 小時的時間。")
                )
            else:
                self.booking_time = booking_time
                self.next_state()

        except ValueError:
            logger.warning(f"[FSM] 時間格式錯誤：{user_input}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="❌ 時間格式錯誤，請輸入 HH:MM，例如 13:00")
            )

    def process_time(self, event):
        self.booking_time = event.message.text
        confirmation_text = (
            "請確認您的預約：\n"
            f"類別：{self.booking_category}\n"
            f"項目：{self.booking_service}\n"
            f"日期：{self.booking_date}\n"
            f"時間：{self.booking_time}\n\n"
            "輸入 '確認' 以完成預約，或輸入 '取消' 以取消。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=confirmation_text))
        self.next_state()

    def confirm_booking(self, event):
        logger.info("[FSM] 進入預約確認階段")
        msg = (
            f"請確認您的預約：\n"
            f"類別：{self.booking_category}\n"
            f"項目：{self.booking_service}\n"
            f"日期：{self.booking_date}\n"
            f"時間：{self.booking_time}\n\n"
            "輸入『確認』以完成預約，或輸入『取消』以取消。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    def process_booking(self, event):
        if event.message.text.lower() == "確認":
            try:
                client = get_gspread_client()
                spreadsheet_key = (
                    SPREADSHEET_KEY  # 您的主要試算表 Key (假設所有資料在同一個試算表的不同工作表)
                )

                # 定義類別與試算表工作表名稱的對應關係
                category_to_sheet = {
                    "預約團體課程": "課程資料",
                    "預約私人教練": "教練資料",
                    "場地租借": "場地資料",
                }

                worksheet_name = category_to_sheet.get(self.booking_category)

                if worksheet_name:
                    sheet = client.open_by_key(spreadsheet_key).worksheet(
                        worksheet_name
                    )
                    booking_data = [
                        self.user_id,
                        self.booking_category,
                        self.booking_service,
                        self.booking_date,
                        self.booking_time,
                    ]
                    sheet.append_row(booking_data)
                    line_bot_api.push_message(
                        self.user_id,
                        TextSendMessage(text=f"✅ 您的 {self.booking_category} 預約已成功記錄！"),
                    )
                else:
                    line_bot_api.push_message(
                        self.user_id,
                        TextSendMessage(text="⚠ 無法確定要將此預約記錄到哪個工作表。"),
                    )
                    logger.error(f"未知的預約類別：{self.booking_category}")

            except Exception as e:
                logger.error(f"儲存預約資料到 Google Sheets 失敗：{e}", exc_info=True)
                line_bot_api.push_message(
                    self.user_id,
                    TextSendMessage(text="⚠ 儲存預約資料時發生錯誤，請稍後再試。"),
                )

            if self.user_id in user_states:
                del user_states[self.user_id]
            self.reset_booking_data()  # 預約完成後重置資料
            self.go_back(2)  # 回到初始狀態 (start_booking)

        elif event.message.text.lower() == "取消":
            self.trigger("cancel_booking", event)
        else:
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text="請輸入 '確認' 或 '取消'。")
            )

    def send_cancellation_message(self, event):
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="❌ 您的預約已取消。")
        )
        if self.user_id in user_states:
            del user_states[self.user_id]
        self.reset_booking_data()  # 取消後重置資料
        self.go_back(2)  # 回到初始狀態 (start_booking)

    def send_booking_start_message(self, event):
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text="好的，請按照步驟完成預約。")
        )
        self.next_state()  # 切換到 category_selection

def ask_coach_name(self, event):
    self.selected_expertise = event.message.text.strip()
    logger.info(f"[FSM] 使用者選擇的教練專長：{self.selected_expertise}")

    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_KEY).worksheet("私人教練")
        records = sheet.get_all_records()

        coach_list = sorted(set(
            row["教練姓名"] for row in records
            if row.get("專長") == self.selected_expertise and row.get("教練姓名")
        ))

        if not coach_list:
            raise ValueError("找不到符合的教練")

        buttons = [MessageAction(label=coach, text=coach) for coach in coach_list[:4]]
        if len(coach_list) > 4:
            self.ask_service(event, coach_list)
        else:
            template = TemplateSendMessage(
                alt_text="請選擇教練",
                template=ButtonsTemplate(
                    title="私人教練 - 教練選擇",
                    text=f"您想預約哪位{self.selected_expertise}的教練？",
                    actions=buttons
                )
            )
            line_bot_api.reply_message(event.reply_token, template)

        self.next_state()
    except Exception as e:
        logger.error(f"❌ 載入教練清單失敗：{e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 找不到對應的教練，請稍後再試。"))
        self.go_back()

def is_personal_coach_category(self):
    return self.booking_category == "私人教練"

def set_selected_coach(self, event):
    self.selected_service = event.message.text.strip()
    self.next_state()
def get_gspread_client():
    credentials_content = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_CONTENT")
    if not credentials_content:
        logger.error("缺少 GOOGLE_APPLICATION_CREDENTIALS_CONTENT 環境變數")
        raise ValueError("環境變數未設定")
    try:
        with tempfile.NamedTemporaryFile(mode="w+", delete=True, suffix=".json") as temp_file:
            temp_file.write(credentials_content)
            temp_file.flush()
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = ServiceAccountCredentials.from_json_keyfile_name(temp_file.name, scope)
            client = gspread.authorize(creds)
            return client
    except Exception as e:
        logger.error(f"Google Sheets 授權錯誤：{e}", exc_info=True)
        sys.exit(1)
@app.route("/")
def home():
    return "LINE Bot 正常運作中！"

@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()
    logger.info(f"使用者 {user_id} 傳送訊息：{user_msg}")
    # 會員專區選單
    if user_msg == "會員專區":
        template = TemplateSendMessage(
            alt_text="會員功能選單",
            template=ButtonsTemplate(
                title="會員專區",
                text="請選擇功能",
                actions=[
                    MessageAction(label="查詢會員資料", text="查詢會員資料"),
                ]
            )
        )
        line_bot_api.reply_message(event.reply_token, template)

    elif user_msg == "查詢會員資料":
        user_states[user_id] = "awaiting_member_info"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入您的會員編號或姓名：")
        )

    elif user_states.get(user_id) == "awaiting_member_info":
        user_states.pop(user_id)
        keyword = user_msg.strip()
    
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("會員資料")
            records = sheet.get_all_records()
    
            # 判斷輸入是編號還是姓名
            if re.match(r"^[A-Z]\d{5}$", keyword.upper()):  # 判斷是 A00001 類型
                member_data = next(
                    (row for row in records if str(row["會員編號"]).strip().upper() == keyword.upper()),
                    None
                )
            else:
                member_data = next(
                    (row for row in records if keyword in row["姓名"]),
                    None
                )
            if member_data:
                reply_text = (
                    f"✅ 查詢成功\n"
                    f"👤 姓名：{member_data['姓名']}\n"
                    f"📱 電話：{member_data['電話']}\n"
                    f"🧾 會員類型：{member_data['會員類型']}\n"
                    f"📌 狀態：{member_data['會員狀態']}\n"
                    f"🎯 點數：{member_data['會員點數']}\n"
                    f"⏳ 到期日：{member_data['會員到期日']}"
                )
                flex_message = FlexSendMessage(
                    alt_text=f"{member_data['姓名']}的會員資料",
                )
            else:
                reply_text = "❌ 查無此會員資料，請確認後再試一次。"
    
        except Exception as e:
            reply_text = f"❌ 查詢失敗：{str(e)}"
            logger.error(f"會員查詢錯誤：{e}", exc_info=True)
    
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        
    if user_msg == "常見問題":
        faq_categories = ["準備運動", "會員方案", "課程", "其他"]
        buttons = [
            MessageAction(label=cat, text=cat)
            for cat in faq_categories
        ]
        template = TemplateSendMessage(
            alt_text="常見問題分類",
            template=ButtonsTemplate(
                title="常見問題",
                text="請選擇分類",
                actions=buttons[:4]  # ButtonsTemplate 最多只能放 4 個按鈕
            )
        )
        line_bot_api.reply_message(event.reply_token, template)

    elif user_msg in ["課程"]:
        confirm_template = TemplateSendMessage(
            alt_text = 'confirm template',
            template = ConfirmTemplate(
                text = '🧾',
                actions = [
                    MessageAction(
                        label = '個人教練',
                        text = '個人教練課程'),
                    MessageAction(
                        label = '團體',
                        text = '團體課程')]
                )
            )
        line_bot_api.reply_message(event.reply_token, confirm_template)

    elif user_msg in ["準備運動", "會員方案", "個人教練課程", "團體課程", "其他"]:
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("常見問題")
            records = sheet.get_all_records()
            matched = [row for row in records if row["分類"] == user_msg]

            if not matched:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到相關問題。"))
                return

            bubbles = []
            for item in matched:
                bubble = {
                    "type": "bubble",
                    "size": "mega",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "text",
                                "text": f"❓ {item['問題']}",
                                "wrap": True,
                                "weight": "bold",
                                "size": "md",
                                "color": "#333333"
                            },
                            {
                                "type": "text",
                                "text": f"💡 {item['答覆']}",
                                "wrap": True,
                                "size": "sm",
                                "color": "#666666"
                            }
                        ]
                    }
                }
                bubbles.append(bubble)

            flex_message = FlexSendMessage(
                alt_text=f"{user_msg} 的常見問題",
                contents={
                    "type": "carousel",
                    "contents": bubbles[:10]  # 最多 10 筆
                }
            )
            line_bot_api.reply_message(event.reply_token, flex_message)

        except Exception as e:
            logger.error(f"常見問題查詢錯誤：{e}", exc_info=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠ 查詢失敗，請稍後再試。"))
            
    elif user_msg == "更多功能":
        flex_message = FlexSendMessage(
            alt_text="更多功能選單",
            contents={
                "type": "carousel",
                "contents": [
                    {
                        "type": "bubble",
                        "hero": {
                            "type": "image",
                            "url": "https://i.imgur.com/d3v7RxR.png",  # 替換為場地圖片
                            "size": "full",
                            "aspectRatio": "20:13",
                            "aspectMode": "cover"
                        },
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "🏟️ 場地介紹",
                                    "weight": "bold",
                                    "size": "xl"
                                },
                                {
                                    "type": "text",
                                    "text": "探索我們的健身空間",
                                    "size": "sm",
                                    "wrap": True,
                                    "color": "#666666"
                                }
                            ]
                        },
                        "footer": {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "sm",
                            "contents": [
                                {
                                    "type": "button",
                                    "action": {
                                        "type": "message",
                                        "label": "健身/重訓",
                                        "text": "健身/重訓"
                                    },
                                    "style": "primary"
                                },
                                {
                                    "type": "button",
                                    "action": {
                                        "type": "message",
                                        "label": "上課教室",
                                        "text": "上課教室"
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "type": "bubble",
                        "hero": {
                            "type": "image",
                            "url": "https://i.imgur.com/HrtfSdH.png",  # 替換為課程圖片
                            "size": "full",
                            "aspectRatio": "20:13",
                            "aspectMode": "cover"
                        },
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "📚 課程介紹",
                                    "weight": "bold",
                                    "size": "xl"
                                },
                                {
                                    "type": "text",
                                    "text": "了解我們提供的課程類型",
                                    "size": "sm",
                                    "wrap": True,
                                    "color": "#666666"
                                }
                            ]
                        },
                        "footer": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "button",
                                    "action": {
                                        "type": "message",
                                        "label": "查看課程內容",
                                        "text": "課程內容"
                                    },
                                    "style": "primary"
                                }
                            ]
                        }
                    },
                    {
                        "type": "bubble",
                        "hero": {
                            "type": "image",
                            "url": "https://i.imgur.com/izThqNv.png",  # 替換為團隊圖片
                            "size": "full",
                            "aspectRatio": "20:13",
                            "aspectMode": "cover"
                        },
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "👥 團隊介紹",
                                    "weight": "bold",
                                    "size": "xl"
                                },
                                {
                                    "type": "text",
                                    "text": "認識我們的教練與團隊",
                                    "size": "sm",
                                    "wrap": True,
                                    "color": "#666666"
                                }
                            ]
                        },
                        "footer": {
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "sm",
                            "contents": [
                                {
                                    "type": "button",
                                    "action": {
                                        "type": "message",
                                        "label": "健身教練",
                                        "text": "健身教練"
                                    },
                                    "style": "primary"
                                },
                                {
                                    "type": "button",
                                    "action": {
                                        "type": "message",
                                        "label": "課程老師",
                                        "text": "課程老師"
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )
        line_bot_api.reply_message(event.reply_token, flex_message)

    elif user_msg == "上課教室":
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("場地資料")
            records = sheet.get_all_records()

            matched = [
                row for row in records
                if row.get("類型", "").strip() == "上課教室" and row.get("圖片1", "").startswith("https")
            ]

            if not matched:
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="⚠ 查無『上課教室』的場地資料")
                )
                return

            image_columns = [
                ImageCarouselColumn(
                    image_url=row["圖片1"],
                    action=MessageAction(label=row.get("名稱", "查看詳情"), text=row.get("名稱", "查看詳情"))
                ) for row in matched
            ]

            carousel = TemplateSendMessage(
                alt_text="上課教室場地列表",
                template=ImageCarouselTemplate(columns=image_columns[:10])
            )
            line_bot_api.reply_message(event.reply_token, carousel)

        except Exception as e:
            logger.error(f"上課教室查詢失敗：{e}", exc_info=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠ 發生錯誤：{e}"))
            
    elif user_msg == "健身/重訓":
        # 顯示分類選單（按鈕）
        subcategories = ["心肺訓練", "背部訓練", "腿部訓練", "自由重量器材"]
        buttons = [
            MessageAction(label=sub, text=sub)
            for sub in subcategories[:4]  # 先顯示前4個
        ]
        # 第二個 bubble 可加更多分類
        template = TemplateSendMessage(
            alt_text="健身/重訓 器材分類",
            template=ButtonsTemplate(
                title="健身/重訓 器材分類",
                text="請選擇器材分類",
                actions=buttons
            )
        )
        line_bot_api.reply_message(event.reply_token, template)
        
    elif user_msg in ["心肺訓練", "背部訓練", "腿部訓練", "自由重量器材"]:
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("場地資料")
            records = sheet.get_all_records()

            matched = [
                row for row in records
                if row.get("分類", "").strip() == user_msg and row.get("圖片1", "").startswith("https")
            ]

            if not matched:
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=f"⚠ 查無『{user_msg}』分類的器材圖片")
                )
                
                return

            # 每 10 筆一組發送
            for i in range(0, len(matched), 10):
                chunk = matched[i:i + 10]
                image_columns = [
                    ImageCarouselColumn(
                        image_url=row["圖片1"],
                        action=MessageAction(label=row.get("名稱", "查看詳情"), text=row.get("名稱", "查看詳情"))
                    ) for row in chunk
                ]

                carousel = TemplateSendMessage(
                    alt_text=f"{user_msg} 器材圖片",
                    template=ImageCarouselTemplate(columns=image_columns)
                )
                line_bot_api.reply_message(event.reply_token, carousel)

        except Exception as e:
            logger.error(f"{user_msg} 分類查詢錯誤：{e}", exc_info=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠ 發生錯誤，請稍後再試。"))
    elif user_msg == "上課教室":
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("場地資料")
            records = sheet.get_all_records()

            matched = [
                row for row in records
                if row.get("類型", "").strip() == "上課教室" and row.get("圖片1", "").startswith("https")
            ]

            if not matched:
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="⚠ 查無『上課教室』的場地資料")
                )
                return

            image_columns = [
                ImageCarouselColumn(
                    image_url=row["圖片1"],
                    action=MessageAction(label=row.get("名稱", "查看詳情"), text=row.get("名稱", "查看詳情"))
                ) for row in matched
            ]

            carousel = TemplateSendMessage(
                alt_text="上課教室場地列表",
                template=ImageCarouselTemplate(columns=image_columns[:10])
            )
            line_bot_api.reply_message(event.reply_token, carousel)

        except Exception as e:
            logger.error(f"上課教室查詢失敗：{e}", exc_info=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠ 發生錯誤：{e}"))
            
    elif user_msg == "健身教練":
         try:
             client = get_gspread_client()
             sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("教練資料")
             records = sheet.get_all_records()
 
             matched = [
                 row for row in records
                 if row.get("教練類型", "").strip() == "健身教練" and row.get("圖片", "").startswith("https")
             ]
 
             if not matched:
                 line_bot_api.reply_message(
                     event.reply_token, TextSendMessage(text="⚠ 查無『健身教練』的資料")
                 )
                 return
 
             bubbles = []
             for row in matched:
                 bubble = {
                     "type": "bubble",
                     "hero": {
                         "type": "image",
                         "url": row["圖片"],
                         "size": "full",
                         "aspectRatio": "20:13",
                         "aspectMode": "cover"
                     },
                     "body": {
                         "type": "box",
                         "layout": "vertical",
                         "spacing": "sm",
                         "contents": [
                             {
                                 "type": "text",
                                 "text": f"{row['姓名']}（{row['教練類別']}）",
                                 "weight": "bold",
                                 "size": "lg",
                                 "wrap": True
                             },
                             {
                                 "type": "text",
                                 "text": f"專長：{row.get('專長', '未提供')}",
                                 "size": "sm",
                                 "wrap": True,
                                 "color": "#666666"
                             }
                         ]
                     },
                     "footer": {
                         "type": "box",
                         "layout": "vertical",
                         "spacing": "sm",
                         "contents": [
                             {
                                 "type": "button",
                                 "style": "primary",
                                 "action": {
                                     "type": "message",
                                     "label": "立即預約",
                                     "text": f"我要預約 {row['姓名']}"
                                 }
                             }
                         ]
                     }
                 }
                 bubbles.append(bubble)
 
             flex_message = FlexSendMessage(
                 alt_text="健身教練清單",
                 contents={
                     "type": "carousel",
                     "contents": bubbles[:10]
                 }
             )
             line_bot_api.reply_message(event.reply_token, flex_message)
 
         except Exception as e:
             logger.error(f"健身教練查詢失敗：{e}", exc_info=True)
             line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠ 發生錯誤：{e}"))

    elif user_msg == "課程教練":
        # 顯示分類選單（按鈕）
        subcategories = ["有氧教練", "瑜珈老師", "游泳教練"]
        buttons = [
            MessageAction(label=sub, text=sub)
            for sub in subcategories[:4]  # 先顯示前4個
        ]
        # 第二個 bubble 可加更多分類
        template = TemplateSendMessage(
            alt_text="課程教練分類",
            template=ButtonsTemplate(
                title="課程教練分類",
                text="請選擇課程教練",
                actions=buttons
            )
        )
        line_bot_api.reply_message(event.reply_token, template)
        
    elif user_msg in ["有氧教練", "瑜珈老師", "游泳教練"]:
         try:
             client = get_gspread_client()
             sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("教練資料")
             records = sheet.get_all_records()
 
             matched = [
                 row for row in records
                 if row.get("教練類別", "").strip() == user_msg and row.get("圖片", "").startswith("https")
             ]
 
             if not matched:
                 line_bot_api.reply_message(
                     event.reply_token, TextSendMessage(text="⚠ 查無『{user_msg}』的資料")
                 )
                 return
 
             bubbles = []
             for row in matched:
                 bubble = {
                     "type": "bubble",
                     "hero": {
                         "type": "image",
                         "url": row["圖片"],
                         "size": "full",
                         "aspectRatio": "20:13",
                         "aspectMode": "cover"
                     },
                     "body": {
                         "type": "box",
                         "layout": "vertical",
                         "spacing": "sm",
                         "contents": [
                             {
                                 "type": "text",
                                 "text": f"{row['姓名']}（{row['教練類別']}）",
                                 "weight": "bold",
                                 "size": "lg",
                                 "wrap": True
                             },
                             {
                                 "type": "text",
                                 "text": f"專長：{row.get('專長', '未提供')}",
                                 "size": "sm",
                                 "wrap": True,
                                 "color": "#666666"
                             }
                         ]
                     },
                     "footer": {
                         "type": "box",
                         "layout": "vertical",
                         "spacing": "sm",
                         "contents": [
                             {
                                 "type": "button",
                                 "style": "primary",
                                 "action": {
                                     "type": "message",
                                     "label": "立即預約",
                                     "text": f"我要預約 {row['姓名']}"
                                 }
                             }
                         ]
                     }
                 }
                 bubbles.append(bubble)
 
             flex_message = FlexSendMessage(
                 alt_text="課程教練清單",
                 contents={
                     "type": "carousel",
                     "contents": bubbles[:10]
                 }
             )
             line_bot_api.reply_message(event.reply_token, flex_message)
 
         except Exception as e:
             logger.error(f"課程教練查詢失敗：{e}", exc_info=True)
             line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠ 發生錯誤：{e}"))
            
    elif user_msg == "課程內容":
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("課程資料")
            records = sheet.get_all_records()

            # 提取唯一課程類型
            course_types = list({row["課程類型"].strip() for row in records if row.get("課程類型")})
            course_types = [t for t in course_types if t]

            # 建立按鈕
            buttons = [
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "message",
                        "label": t,
                        "text": t
                    }
                } for t in course_types[:6]
            ]

            bubble = {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": "📚 課程內容查詢",
                            "weight": "bold",
                            "size": "lg",
                            "margin": "md"
                        },
                        {
                            "type": "box",
                            "layout": "vertical",
                            "spacing": "sm",
                            "margin": "lg",
                            "contents": buttons
                        }
                    ]
                }
            }

            flex_msg = FlexSendMessage(
                alt_text="課程類型查詢",
                contents=bubble
            )

            line_bot_api.reply_message(
                event.reply_token,
                [
                    flex_msg,
                    TextSendMessage(text="📅 你也可以輸入日期（例如：2025-05-01）查詢當天開課課程。")
                ]
            )

        except Exception as e:
            logger.error(f"課程內容查詢錯誤：{e}", exc_info=True)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠ 無法讀取課程資料"))

    elif user_msg in ["有氧課程", "瑜珈課程", "游泳課程"]:
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("課程資料")
            records = sheet.get_all_records()

            matched = [row for row in records if row.get("課程類型", "").strip() == user_msg]

            if not matched:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 查無『{user_msg}』相關課程"))
                return

            bubbles = []
            for row in matched[:10]:
                bubbles.append({
                    "type": "bubble",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": row.get("課程名稱", "（未提供課程名稱）"), "weight": "bold", "size": "lg", "wrap": True},
                            {"type": "text", "text": f"👨‍🏫 教練：{row.get('教練姓名', '未知')}", "size": "sm", "wrap": True},
                            {"type": "text", "text": f"📅 開課日期：{row.get('開始日期', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"🕒 上課時間：{row.get('上課時間', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"⏱️ 時間：{row.get('時間', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"💲 價格：{row.get('課程價格', '未定')}", "size": "sm"}
                        ]
                    }
                })

            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text=f"{user_msg} 課程內容",
                    contents={"type": "carousel", "contents": bubbles}
                )
            )

        except Exception as e:
            logger.error(f"課程類型查詢錯誤：{e}", exc_info=True)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠ 無法查詢課程內容（錯誤：{str(e)}）")
            )

    elif re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", user_msg):
        query_date = user_msg.replace("/", "-").strip()
        try:
            client = get_gspread_client()
            sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("課程資料")
            records = sheet.get_all_records()

            matched = [row for row in records if row.get("開始日期", "").strip() == query_date]

            if not matched:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 該日期無任何課程"))
                return

            bubbles = []
            for row in matched[:10]:
                bubbles.append({
                    "type": "bubble",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": row.get("課程名稱", "（未提供課程名稱）"), "weight": "bold", "size": "lg", "wrap": True},
                            {"type": "text", "text": f"👨‍🏫 教練：{row.get('教練姓名', '未知')}", "size": "sm", "wrap": True},
                            {"type": "text", "text": f"📅 開課日期：{row.get('開始日期', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"🕒 上課時間：{row.get('上課時間', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"⏱️ 時間：{row.get('時間', '未提供')}", "size": "sm"},
                            {"type": "text", "text": f"💲 價格：{row.get('課程價格', '未定')}", "size": "sm"}
                        ]
                    }
                })

            line_bot_api.reply_message(
                event.reply_token,
                FlexSendMessage(
                    alt_text=f"{query_date} 的課程",
                    contents={"type": "carousel", "contents": bubbles}
                )
            )

        except Exception as e:
            logger.error(f"課程日期查詢錯誤：{e}", exc_info=True)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠ 無法查詢課程內容（錯誤訊息：{str(e)}）")
            )

    elif user_msg == "健身紀錄":
        liff_url = "https://liffweb.vercel.app/"  # 這是新專案上線的網址
        flex_message = FlexSendMessage(
            alt_text="健身紀錄",
            contents={
                "type": "bubble",
                "hero": {
                    "type": "image",
                    "url": "https://example.com/your_new_image.jpg",  # 替換成您的新圖片網址
                    "size": "full",
                    "aspectRatio": "20:13",
                    "aspectMode": "cover",
                    "action": {
                        "type": "uri",
                        "uri": liff_url
                    }
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "height": "md",
                            "action": {
                                "type": "uri",
                                "label": "開始記錄今日健身！",
                                "uri": liff_url
                            }
                        }
                    ]
                }
            }
        )
        line_bot_api.reply_message(event.reply_token, flex_message)
    elif user_msg == "我要預約":
        if user_id not in user_states or not isinstance(user_states[user_id], BookingFSM):
            if user_states.get(user_id) == "awaiting_member_check_before_booking":
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="請先輸入您的姓名以進行驗證。")
                )
            else:
                user_states[user_id] = "awaiting_member_check_before_booking"
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="您好，請先輸入您的姓名以進行預約。")
                )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您已經在預約流程中，請繼續操作。"))

    elif user_states.get(user_id) == "awaiting_member_check_before_booking":
        user_states.pop(user_id)
        keyword = user_msg.strip()
        logger.info(f"User {user_id}: 預約驗證 - 使用者輸入姓名: '{keyword}'")

        try:
            client = get_gspread_client()
            sheet = client.open_by_key(SPREADSHEET_KEY).worksheet("會員資料")
            records = sheet.get_all_records()

            member_data = next(
                (row for row in records if keyword in row["姓名"]),
                None
            )

            if member_data:
                logger.info(f"User {user_id}: 預約驗證 - 找到會員: {member_data['姓名']}")

            # 建立 FSM 狀態機並啟動流程
                states = [
                            'start_booking',
                            'category_selection',
                            'expertise_selection',   # 新增：私人教練專長
                            'coach_selection',       # 新增：教練姓名
                            'service_selection',
                            'date_input',
                            'time_input',
                            'confirmation',
                            'completed',
                            'cancelled'
                            ]
                transitions = [
                                {'trigger': 'start', 'source': 'start_booking', 'dest': 'category_selection', 'after': 'ask_category'},
                                    # 根據是否為私人教練，走不同流程
                                {'trigger': 'select_category', 'source': 'category_selection', 'dest': 'expertise_selection',
                                 'conditions': 'is_personal_coach_category', 'after': 'ask_expertise'},
                                {'trigger': 'select_category', 'source': 'category_selection', 'dest': 'service_selection',
                                 'unless': 'is_personal_coach_category', 'after': 'process_category'},
                                # 專長 → 教練 → 項目（實際上選完教練就可以定義 service）
                                {'trigger': 'select_expertise', 'source': 'expertise_selection', 'dest': 'coach_selection', 'after': 'ask_coach_name'},
                                {'trigger': 'select_coach', 'source': 'coach_selection', 'dest': 'date_input', 'after': 'ask_date'},
                                    # 非私人教練的流程：項目 → 日期
                                {'trigger': 'select_service', 'source': 'service_selection', 'dest': 'date_input', 'after': 'ask_date'},
                                {'trigger': 'enter_date', 'source': 'date_input', 'dest': 'time_input', 'after': 'ask_time'},
                                {'trigger': 'enter_time', 'source': 'time_input', 'dest': 'confirmation', 'after': 'process_time'},
                                {'trigger': 'confirm_booking', 'source': 'confirmation', 'dest': 'completed', 'after': 'process_booking'},
                                {'trigger': 'cancel_booking', 'source': '*', 'dest': 'cancelled', 'after': 'send_cancellation_message'},
                                {'trigger': 'restart_booking', 'source': '*', 'dest': 'start_booking', 'after': 'send_booking_start_message'}
                            ]
                fsm = BookingFSM(user_id, states=states, transitions=transitions, initial='start_booking')
                fsm.member_name = member_data['姓名']  # ✅ 儲存會員姓名
                user_states[user_id] = fsm
                fsm.start(event)  # ✅ 啟動預約流程

            else:
                logger.info(f"User {user_id}: 預約驗證 - 查無此會員")
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="❌ 查無此會員資料，請確認後再試一次。")
                )

        except Exception as e:
            logger.error(f"❌ 會員驗證失敗，試算表 KEY：{SPREADSHEET_KEY}，錯誤類型：{type(e).__name__}, 錯誤內容：{e}", exc_info=True)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"❌ 會員驗證失敗，請稍後再試。")
            )
    else:
        fsm = user_states.get(user_id)
        if isinstance(fsm, BookingFSM):
            if fsm.state == "category_selection":
                fsm.select_category(event)
            elif fsm.state == "service_selection":
                fsm.select_service(event)
            elif fsm.state == "date_input":
                fsm.enter_date(event)
            elif fsm.state == "time_input":
                fsm.enter_time(event)
            elif fsm.state == "confirmation":
                fsm.confirm_booking(event)
            else:
                logger.warning(f"[FSM] 使用者 {user_id} 處於未知狀態：{fsm.state}")
        else:
            try:
                client = get_gspread_client()
                sheet = client.open_by_key("1jVhpPNfB6UrRaYZjCjyDR4GZApjYLL4KZXQ1Si63Zyg").worksheet("場地資料")
                records = sheet.get_all_records()

                matched = next((row for row in records if row.get("名稱") == user_msg), None)

                if matched and matched.get("圖片1", "").startswith("https"):
                    bubble = {
                        "type": "bubble",
                        "hero": {
                            "type": "image",
                            "url": matched["圖片1"],
                            "size": "full",
                            "aspectRatio": "20:13",
                            "aspectMode": "cover"
                        },
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "spacing": "sm",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": matched["名稱"],
                                    "weight": "bold",
                                    "size": "xl",
                                    "wrap": True
                                },
                                {
                                    "type": "text",
                                    "text": matched["描述"],
                                    "size": "sm",
                                    "wrap": True,
                                    "color": "#666666"
                                }
                            ]
                        }
                    }

                    flex_msg = FlexSendMessage(
                        alt_text=f"{matched['名稱']} 詳細資訊",
                        contents=bubble
                    )
                    line_bot_api.reply_message(event.reply_token, flex_msg)
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無該場地資料"))

            except Exception as e:
                logger.error(f"場地詳情查詢失敗：{e}", exc_info=True)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠ 發生錯誤：{e}"))
load_booking_options()  # 載入預約資料選項
if __name__ == "__main__":
    
    app.run()
