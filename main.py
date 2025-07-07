from typing import Final 
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes  

# Constants
TOKEN: Final = '7814310621:AAHIG6y7lCXVnjAUhYLANEtj0w92eLpDAaw'
BOT_USERNAME: Final = '@linguamaterials_bot'
FORWARD_TO_USER_ID: Final = '827718334'  


# Commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Start command called")  # Debug statement to track executions
    await update.message.reply_text(
        ' LINGUA MATERIALS BOT \n\n This is your space to:\n\n❓ Ask professional questions \n📕 Share interesting, funny, or challenging experiences from your teaching practice \n💬 Start discussions on relevant topics in education \n\nAll stories, questions and ideas will be posted on Instagram (linguamaterials) and Telegram channel (ketro_eng) \n\nWrite your message down👇'
    )
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("find documents")  # Debug statement to track executions
    await update.message.reply_text(
        'Available EMC’s + links on the channel now:\n\n•Kid’s Box New Generation (https://t.me/linguamaterials/11) \n•Kid’s Box Updated Second Edition (https://t.me/linguamaterials/31) \n•Eyes Open (https://t.me/linguamaterials/21) \n• English File 4th Edition (https://t.me/linguamaterials/38) \n• Gateway to the World (https://t.me/linguamaterials/70) \n• SpeakOut 2nd Edition (https://t.me/linguamaterials/120) \n• Wider World 2nd Edition (https://t.me/linguamaterials/143) \n• Go Getter (https://t.me/linguamaterials/150) \n• Family and Friends (https://t.me/linguamaterials/185) \n\nIf you didn’t find the necessary EMC you were looking for, write its name to the bot after the “starts the Bot” command. We will try to search for it and post it soon.'
    )

# Response handler
def handle_response(text: str) -> str:
    processed: str = text.lower()
    if len(processed) < 10000:
        response = "Thank you! Stay tuned for your question to be featured on our Instagram page: @linguamaterials. All submissions remain completely anonymous. 👀 \nTo submit another question, feel free to send the /start command again."
    else:
        response = "Thank you for sharing your story with me!"
    return response       

# Function to forward messages to your personal Telegram account
async def forward_message_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    await context.bot.send_message(chat_id=FORWARD_TO_USER_ID, text=f"User {update.message.chat.id} sent: {message_text}")

# Message handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_type: str = update.message.chat.type
    text: str = update.message.text

    print(f'User ({update.message.chat.id}) in {message_type}: "{text}"')

    if message_type == 'group':
        if BOT_USERNAME in text:
            new_text: str = text.replace(BOT_USERNAME, '').strip()
            response: str = handle_response(new_text)
        else:
            return
    else:
        response: str = handle_response(text)

    # Forward message to your user ID
    await forward_message_to_user(update, context)

    print('BOT:', response)
    await update.message.reply_text(response)             

# Error handler
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')

# Main code
if __name__ == '__main__':
    print('Starting bot...')
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start_command))
    #find
    app.add_handler(CommandHandler('emc', find_command))
    # Messages
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    # Errors
    app.add_error_handler(error)

    # Poll the bot
    print('Polling...')
    app.run_polling()
