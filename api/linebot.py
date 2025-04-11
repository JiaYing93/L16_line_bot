from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage, TemplateSendMessage, CarouselColumn,
                            CarouselTemplate, MessageAction, URIAction, ImageCarouselColumn, ImageCarouselTemplate,
                            ImageSendMessage, ButtonsTemplate, ConfirmTemplate)
import os
import requests
from bs4 import BeautifulSoup
import random

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
line_handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

app = Flask(__name__)

@app.route('/')
def home():
    return 'Hello, World!'

@app.route("/webhook", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    # handle webhook body
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):

    if event.message.text == 'confirm':
        confirm_template = TemplateSendMessage(
            alt_text = 'confirm template',
            template = ConfirmTemplate(
                text = 'drink coffee?',
                actions = [
                    MessageAction(
                        label = 'yes',
                        text = 'yes'),
                    MessageAction(
                        label = 'no',
                        text = 'no')]
                )
            )
        line_bot_api.reply_message(event.reply_token, confirm_template)


    #按鈕樣板
    if event.message.text == '按鈕':
        buttons_template = TemplateSendMessage(
            alt_text = 'buttons template',
            template = ButtonsTemplate(
                thumbnail_image_url='https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg',
                title = 'Brown Cafe',
                text = 'Enjoy your coffee',
                actions = [
                    MessageAction(
                        label = '咖啡有什麼好處',
                        text = '讓人有精神'),
                    URIAction(
                        label = '伯朗咖啡',
                        uri = 'https://www.mrbrown.com.tw/')]
                )
            )

        line_bot_api.reply_message(event.reply_token, buttons_template)


    #carousel樣板
    if event.message.text == 'carousel':
        carousel_template = TemplateSendMessage(
            alt_text = 'carousel template',
            template = CarouselTemplate(
                columns = [
                    #第一個
                    CarouselColumn(
                        thumbnail_image_url = 'https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg',
                        title = 'this is menu1',
                        text = 'menu1',
                        actions = [
                            MessageAction(
                                label = '咖啡有什麼好處',
                                text = '讓人有精神'),
                            URIAction(
                                label = '伯朗咖啡',
                                uri = 'https://www.mrbrown.com.tw/')]),
                    #第二個
                    CarouselColumn(
                        thumbnail_image_url = 'https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg',
                        title = 'this is menu2',
                        text = 'menu2',
                        actions = [
                            MessageAction(
                                label = '咖啡有什麼好處',
                                text = '讓人有精神'),
                            URIAction(
                                label = '伯朗咖啡',
                                uri = 'https://www.mrbrown.com.tw/')])
                ])
            )

        line_bot_api.reply_message(event.reply_token, carousel_template)


    #image carousel樣板
    if event.message.text == 'image carousel':
        image_carousel_template = TemplateSendMessage(
            alt_text = 'image carousel template',
            template = ImageCarouselTemplate(
                columns = [
                    #第一張圖
                    ImageCarouselColumn(
                        image_url = 'https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg',
                        action = URIAction(
                            label = '伯朗咖啡',
                            uri = 'https://www.mrbrown.com.tw/')),
                    #第二張圖
                    ImageCarouselColumn(
                        image_url = 'https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg',
                        action = URIAction(
                            label = '伯朗咖啡',
                            uri = 'https://www.mrbrown.com.tw/'))                       
                ])
            )

        line_bot_api.reply_message(event.reply_token, image_carousel_template)

if __name__ == "__main__":
    app.run()
