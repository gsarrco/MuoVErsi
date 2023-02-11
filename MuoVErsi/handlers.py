import logging
import os
import re
import sys
from datetime import datetime

import yaml
from babel.dates import format_date
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, KeyboardButton, InlineKeyboardMarkup, \
    InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters, CallbackQueryHandler,
)

from .db import DBFile
from .helpers import StopData, get_time

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

current_dir = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(current_dir + "/../")
thismodule = sys.modules[__name__]
thismodule.aut_db_con = None
thismodule.nav_db_con = None

SPECIFY_STOP, SEARCH_STOP, SHOW_STOP, FILTER_TIMES = range(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Benvenuto su MuoVErsi, uno strumento avanzato per chi prende i trasporti pubblici a Venezia.\n\n"
        "Inizia la tua ricerca con /fermata_aut per il servizio automobilistico, o /fermata_nav per quello di navigazione."
    )


async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    reply_keyboard = [['Automobilistico', 'Navigazione']]
    await update.message.reply_text(
        "Quale servizio ti interessa?",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True, input_field_placeholder="Servizio"
        )
    )

    return SPECIFY_STOP


async def specify_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_lower = update.message.text.lower()
    if message_lower == 'automobilistico':
        context.user_data['transport_type'] = 'automobilistico'
    elif message_lower == 'navigazione':
        context.user_data['transport_type'] = 'navigazione'
    else:
        await update.message.reply_text("Servizio non valido. Riprova.")
        return ConversationHandler.END

    context.user_data['transport_type'] = message_lower
    reply_keyboard = [[KeyboardButton("Invia posizione", request_location=True)]]

    await update.message.reply_text(
        f"Inizia digitando il nome della fermata del servizio {message_lower} oppure invia la posizione attuale per "
        f"vedere le fermate più vicine.\n\n",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True, input_field_placeholder="Posizione attuale"
        )
    )

    return SEARCH_STOP


async def search_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    message = update.message

    if context.user_data['transport_type'] == 'automobilistico':
        cur = thismodule.aut_db_con.cursor()
    else:
        cur = thismodule.nav_db_con.cursor()

    if message.location:
        lat = message.location.latitude
        long = message.location.longitude

        result = cur.execute(
            'SELECT stop_id, stop_name FROM stops ORDER BY ((stop_lat-?)*(stop_lat-?)) + ((stop_lon-?)*(stop_lon-?)) '
            'ASC LIMIT 5',
            (lat, lat, long, long))
    else:
        result = cur.execute('SELECT stop_id, stop_name FROM stops where stop_name LIKE ? LIMIT 5',
                             ('%' + message.text + '%',))

    stop_results = result.fetchall()
    if not stop_results:
        await update.message.reply_text('Non abbiamo trovato la fermata che hai inserito. Riprova.')
        return SEARCH_STOP

    stops = []

    for stop in stop_results:
        stop_id, stop_name = stop
        stoptime_results = cur.execute(
            'SELECT stop_headsign, count(stop_headsign) as headsign_count FROM stop_times WHERE stop_id = ? '
            'GROUP BY stop_headsign ORDER BY headsign_count DESC LIMIT 2;',
            (stop_id,)).fetchall()
        if stoptime_results:
            count = sum([stoptime[1] for stoptime in stoptime_results])
            headsigns = '/'.join([stoptime[0] for stoptime in stoptime_results])
        else:
            count, headsigns = 0, '*NO ORARI*'

        stops.append((stop_id, stop_name, headsigns, count))

    if not message.location:
        stops.sort(key=lambda x: -x[3])
    buttons = [[f'{stop_name} ({stop_id}) - {headsigns}'] for stop_id, stop_name, headsigns, count in stops]

    await update.message.reply_text(
        "Scegli la fermata",
        reply_markup=ReplyKeyboardMarkup(
            buttons, one_time_keyboard=True, input_field_placeholder="Scegli la fermata"
        )
    )

    return SHOW_STOP


async def show_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data['transport_type'] == 'automobilistico':
        con = thismodule.aut_db_con
    else:
        con = thismodule.nav_db_con

    stop_id = re.search(r'.*\((\d+)\).*', update.message.text).group(1)

    now = datetime.now()

    stopdata = StopData(stop_id, now.date(), '', '', '')
    stopdata.save_query_data(context)
    await update.message.reply_text('Ecco gli orari', disable_notification=True,
                                    reply_markup=stopdata.get_days_buttons(context))

    results = stopdata.get_times(con)

    text, reply_markup, times_history = stopdata.format_times_text(results, context.user_data.get('times_history', []))
    context.user_data['times_history'] = times_history
    await update.message.reply_text(text, reply_markup=reply_markup)

    return FILTER_TIMES


async def filter_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data['transport_type'] == 'automobilistico':
        con = thismodule.aut_db_con
    else:
        con = thismodule.nav_db_con

    if update.callback_query:
        query = update.callback_query

        if query.data[0] == 'R':
            trip_id, stop_id, day_raw, stop_sequence, line = query.data[1:].split('/')
            day = datetime.strptime(day_raw, '%Y%m%d').date()

            if context.user_data['transport_type'] == 'automobilistico':
                cur = thismodule.aut_db_con.cursor()
            else:
                cur = thismodule.nav_db_con.cursor()

            sql_query = """SELECT departure_time, stop_name
                                    FROM stop_times
                                             INNER JOIN stops ON stop_times.stop_id = stops.stop_id
                                    WHERE stop_times.trip_id = ?
                                    AND stop_sequence >= ?
                                    ORDER BY stop_sequence"""

            results = cur.execute(sql_query, (trip_id, stop_sequence)).fetchall()

            text = format_date(day, format='full', locale='it') + ' - linea ' + line + '\n'

            for result in results:
                time_raw, stop_name = result
                time_format = get_time(time_raw).isoformat(timespec="minutes")
                text += f'\n{time_format} {stop_name}'

            await query.answer('')
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton('Indietro', callback_data=context.user_data['query_data'])]])
            await query.edit_message_text(text=text, reply_markup=reply_markup)
            return FILTER_TIMES

        logger.info("Query data %s", query.data)
        stopdata = StopData(query_data=query.data)
        stopdata.save_query_data(context)
    else:
        stopdata = StopData(query_data=context.user_data[update.message.text])
        stopdata.save_query_data(context)
    results = stopdata.get_times(con)
    text, reply_markup, times_history = stopdata.format_times_text(results, context.user_data.get('times_history', []))
    context.user_data['times_history'] = times_history

    if update.callback_query:
        await query.answer('')
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=text, reply_markup=reply_markup)
    return FILTER_TIMES


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    context.user_data.clear()
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "Conversazione interrotta. Ti ritrovi nella schermata iniziale di MuoVErsi.\n\n"
        "Inizia la tua ricerca con /fermata_aut per il servizio automobilistico, o /fermata_nav per quello di navigazione.",
        reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END


def main() -> None:
    config_path = os.path.join(parent_dir, 'config.yaml')
    with open(config_path, 'r') as config_file:
        try:
            config = yaml.safe_load(config_file)
            logger.info(config)
        except yaml.YAMLError as err:
            logger.error(err)

    thismodule.aut_db_con = DBFile('automobilistico').connect_to_database()
    thismodule.nav_db_con = DBFile('navigazione').connect_to_database()

    application = Application.builder().token(config['TOKEN']).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("fermata", choose_service)],
        states={
            SPECIFY_STOP: [MessageHandler(filters.TEXT, specify_stop)],
            SEARCH_STOP: [MessageHandler((filters.TEXT | filters.LOCATION) & (~ filters.COMMAND), search_stop)],
            SHOW_STOP: [MessageHandler(filters.Regex(r'.*\((\d+)\).*'), show_stop)],
            FILTER_TIMES: [
                CallbackQueryHandler(filter_times),
                MessageHandler(filters.Regex(r'^\-|\+1g$'), filter_times)
            ]
        },
        fallbacks=[CommandHandler("annulla", cancel), CommandHandler("fermata", choose_service)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    if config.get('DEV', False):
        application.run_polling()
    else:
        application.run_webhook(listen='0.0.0.0', port=443, secret_token=config['SECRET_TOKEN'],
                                webhook_url=config['WEBHOOK_URL'], key=os.path.join(parent_dir, 'private.key'),
                                cert=os.path.join(parent_dir, 'cert.pem'))
