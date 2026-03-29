#!/usr/bin/env python3

import asyncio
import random
import logging
import json
import os
import time
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
from telegram.ext import ChatMemberHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

with open('words.json', 'r', encoding='utf-8') as f:
    WORD_CATEGORIES = json.load(f)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

logger = logging.getLogger(__name__)

# Data file for scores
SCORES_FILE = os.getenv('SCORES_FILE', 'scores.json')
LOGS_CHAT_ID = int(os.getenv('LOGS_CHAT_ID', '-1002252848421'))
GAME_NOTIFICATION_CHAT_ID = int(os.getenv('GAME_NOTIFICATION_CHAT_ID', '-1002771343852'))
USER_DATA_FILE = os.getenv('USER_DATA_FILE', 'user_data.json')
BANNED_USERS_FILE = os.getenv('BANNED_USERS_FILE', 'banned_users.json')

# Load banned users
banned_users = set()
try:
    if os.path.exists(BANNED_USERS_FILE):
        with open(BANNED_USERS_FILE, 'r') as f:
            data = json.load(f)
            banned_users = set(data.get('banned_users', []))
except Exception as e:
    logger.error(f"Failed to load banned users: {e}")
    
# Load / Save scores
if os.path.exists(SCORES_FILE):
    with open(SCORES_FILE, 'r') as f:
        try:
            loaded_scores = json.load(f)
            if isinstance(loaded_scores, dict):
                player_scores = loaded_scores
            else:
                logger.error("scores.json is not a dict, resetting player_scores")
                player_scores = {}
        except json.decoder.JSONDecodeError:
            logger.error("Failed to parse scores.json, resetting player_scores")
            player_scores = {}
else:
    player_scores = {}

# Initial empty sets for users and groups
all_users = set()
private_users = set()
all_group_chats = set()

# Load user data from file
if os.path.exists(USER_DATA_FILE):
    try:
        with open(USER_DATA_FILE, 'r') as f:
            data = json.load(f)
            all_users = set(data.get('all_users', []))
            private_users = set(data.get('private_users', []))
            all_group_chats = set(data.get('all_group_chats', []))
    except Exception as e:
        logger.error(f"Failed to load user_data.json: {e}")

def save_scores():
    with open(SCORES_FILE, 'w') as f:
        json.dump(player_scores, f, indent=4)

def save_user_data():
    data = {
        "all_users": list(all_users),
        "private_users": list(private_users),
        "all_group_chats": list(all_group_chats),
    }
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def update_user_stats(update: Update):
    if not update.effective_user:
        return
    before_users_len = len(all_users)
    all_users.add(update.effective_user.id)
    if update.effective_chat:
        if update.effective_chat.type in ['group', 'supergroup']:
            all_group_chats.add(update.effective_chat.id)
        if update.effective_chat.type == 'private':
            private_users.add(update.effective_user.id)
    if len(all_users) > before_users_len:
        save_user_data()

# Save banned users helper
def save_banned_users():
    with open(BANNED_USERS_FILE, 'w') as f:
        json.dump({"banned_users": list(banned_users)}, f, indent=4)

# Check if user is banned
def is_user_banned(user_id: int) -> bool:
    return user_id in banned_users
    
def update_player_score(user_id: int, name: str, won: bool, won_as_spy: bool, coins: int):
    key = str(user_id)
    if key not in player_scores or not isinstance(player_scores[key], dict):
        player_scores[key] = {
            "name": name,
            "cash": 0,
            "games_played": 0,
            "games_won": 0,
            "spy_wins": 0
        }
    player = player_scores[key]
    # Update name to latest
    player["name"] = name
    player["games_played"] += 1
    if won:
        player["games_won"] += 1
        player["cash"] += coins
        if won_as_spy:
            player["spy_wins"] += 1
    save_scores()

# Game state management
class GameState:
    def __init__(self):
        self.lobby_active = False
        self.game_active = False
        self.host_message_id: Optional[int] = None
        self.chat_id: Optional[int] = None
        self.players: List[tuple] = []
        self.current_round = 0
        self.current_player_index = 0
        self.spy_user_ids: List[int] = []
        self.citizen_word = ""
        self.player_hints: Dict[int, List[str]] = {}
        self.spy_word = ""
        self.votes: Dict[int, int] = {}
        self.voting_active = False
        self.word_messages = {}
        self.round_message_id: Optional[int] = None
        self.admin_user_id: Optional[int] = None
        self.eliminated_players: Set[int] = set()
        self.start_message_id: Optional[int] = None
        self.join_message_id: Optional[int] = None
        self.spy_kill_targets: Dict[int, Optional[int]] = {}
        self.spy_kill_available: bool = False
        self.discussion_active: bool = False
        self.last_hint_message_id: Optional[int] = None
        # Time settings
        self.turn_time: int = 30
        self.discussion_time: int = 30
        self.voting_time: int = 60
        # Message deletion settings
        self.dead_people_can_write = True
        self.delete_word_messages = False
        # Track words for each round
        self.round_words: Dict[int, Dict[str, str]] = {}
        # Track last words eligibility
        self.last_words_eligible: Dict[int, float] = {}
        # Track player roles
        self.player_roles: Dict[int, str] = {}
        self.detective_id: Optional[int] = None
        self.doctor_id: Optional[int] = None
        self.kamikaze_id: Optional[int] = None
        self.hacker_id: Optional[int] = None
        self.gangster_ids: List[int] = []
        # Detective actions
        self.detective_action_available: bool = False
        self.detective_inspect_target: Optional[int] = None
        self.detective_kill_target: Optional[int] = None
        self.detective_inspect_message_id: Optional[int] = None
        # Doctor properties
        self.doctor_save_target: Optional[int] = None
        self.doctor_saved_self: bool = False
        self.doctor_save_message_id: Optional[int] = None
        # Spy kill message tracking
        self.spy_kill_message_ids: Dict[int, int] = {}
        # Kamikaze properties
        self.kamikaze_revenge_target: Optional[int] = None
        self.kamikaze_revenge_timestamp: Optional[float] = None
        self.kamikaze_revenge_message_id: Optional[int] = None
        # Original roles tracking
        self.original_roles: Dict[int, str] = {}

    def reset(self):
        self.lobby_active = False
        self.player_hints = {}
        self.game_active = False
        self.host_message_id = None
        self.chat_id = None
        self.players = []
        self.current_round = 0
        self.current_player_index = 0
        self.spy_user_ids = []
        self.word_messages = {}
        self.citizen_word = ""
        self.spy_word = ""
        self.votes = {}
        self.voting_active = False
        self.round_message_id = None
        self.admin_user_id = None
        self.eliminated_players = set()
        self.start_message_id = None
        self.join_message_id = None
        self.spy_kill_targets = {}
        self.spy_kill_available = False
        self.discussion_active = False
        self.last_hint_message_id = None
        # Reset time settings to defaults
        self.turn_time = 30
        self.discussion_time = 30
        self.voting_time = 60
        # Reset message deletion settings to defaults
        self.dead_people_can_write = True
        self.delete_word_messages = False
        # Reset round words
        self.round_words = {}
        # Reset last words
        self.last_words_eligible = {}
        # Reset roles
        self.player_roles = {}
        self.detective_id = None
        self.doctor_id = None
        self.kamikaze_id = None
        self.hacker_id = None
        self.gangster_ids = []
        # Reset Detective actions
        self.detective_action_available = False
        self.detective_inspect_target = None
        self.detective_kill_target = None
        self.detective_inspect_message_id = None
        # Reset Doctor properties
        self.doctor_save_target = None
        self.doctor_saved_self = False
        self.doctor_save_message_id = None
        # Reset spy kill message tracking
        self.spy_kill_message_ids = {}
        # Reset Kamikaze properties
        self.kamikaze_revenge_target = None
        self.kamikaze_revenge_timestamp = None
        self.kamikaze_revenge_message_id = None
        # Reset original roles
        self.original_roles = {}

# Global game states - one per chat
games = {} # chat_id -> GameState

# Spam cooldown for "already given hint" replies: (chat_id, user_id) -> last_reply_timestamp
_hint_spam_cooldown: Dict[tuple, float] = {}

# The bot creators/admins (can be expanded or added via /admin command)
creators = set([675001209])

# Word categories for the game

def assign_words_for_round(game):
    category = random.choice(list(WORD_CATEGORIES.keys()))
    words_in_category = WORD_CATEGORIES[category].copy()

    citizen_word = random.choice(words_in_category)
    words_in_category.remove(citizen_word)
    spy_word = random.choice(words_in_category)

    game.citizen_word = citizen_word
    game.spy_word = spy_word
    
    # Track words for this round
    game.round_words[game.current_round] = {
        "citizen": citizen_word,
        "spy": spy_word
    }

    for user_id, _ in game.players:
        role = game.player_roles.get(user_id, 'Citizen')
        role_emoji = get_role_emoji(role)
        
        # FIXED: Check original role to differentiate Spy from Gangster
        original_role = game.original_roles.get(user_id, role)
        
        # Original Spy gets spy message
        if original_role == 'Spy':
            game.word_messages[user_id] = f"""You're the Spy {role_emoji}
Your word: {spy_word}
Make others fool, don't let them know you're spy"""
        # Gangsters get gangster message format
        elif original_role == 'Gangster':
            game.word_messages[user_id] = f"""You're {role} {role_emoji}
Your word is: {spy_word}"""
        # All other roles get citizen_word
        else:
            game.word_messages[user_id] = f"""You're {role} {role_emoji}
Your word is: {citizen_word}"""

def get_or_create_game(chat_id: int) -> GameState:
    if chat_id not in games:
        game = GameState()
        game.chat_id = chat_id
        game.delete_word_messages = True  # CHANGE 1: Default to True
        games[chat_id] = game
    return games[chat_id]

def remove_game(chat_id: int):
    if chat_id in games:
        game = games[chat_id]
        # Determine winner before removing
        winner = "Unknown"
        total_rounds = game.current_round
        
        if game.game_active:
            alive_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
            if alive_spies:
                winner = "Spy"
            else:
                winner = "Citizens"
        
        del games[chat_id]
        save_games_to_file()
        
        # Send game end notification asynchronously
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(notify_game_end(None, chat_id, winner, total_rounds))
        except:
            pass

def get_role_emoji(role: str) -> str:
    """Get emoji for role - FIXED: Standardized emojis"""
    role_emojis = {
        'Citizen': '👥',
        'Detective': '🕵️',  # FIXED: Added full emoji with variation selector
        'Spy': '🕶️',        # FIXED: Consistent with messages
        'Doctor': '👨‍⚕️',    # FIXED: Full emoji
        'Kamikaze': '💣',
        'Gangster': '🥷',
        'Hacker': '👨‍💻'
    }
    return role_emojis.get(role, '❓')

def save_games_to_file():
    """Save all active games to livegames.json"""
    try:
        games_data = {}
        for chat_id, game in games.items():
            if game.game_active:
                games_data[str(chat_id)] = {
                    'chat_id': game.chat_id,
                    'players': game.players,
                    'spy_user_ids': list(game.spy_user_ids),
                    'citizen_word': game.citizen_word,
                    'spy_word': game.spy_word,
                    'current_round': game.current_round,
                    'current_player_index': game.current_player_index,
                    'votes': {str(k): v for k, v in game.votes.items()},
                    'eliminated_players': list(game.eliminated_players),
                    'game_active': game.game_active,
                    'voting_active': game.voting_active,
                    'join_message_id': game.join_message_id,
                    'round_message_id': game.round_message_id,
                    'player_hints': {str(k): v for k, v in game.player_hints.items()},
                    'spy_kill_targets': {str(k): v for k, v in game.spy_kill_targets.items()},
                    'spy_kill_available': game.spy_kill_available,
                    'turn_time': game.turn_time,
                    'discussion_time': game.discussion_time,
                    'voting_time': game.voting_time,
                    'delete_word_messages': game.delete_word_messages,
                    'dead_people_can_write': game.dead_people_can_write,
                    'round_words': {str(k): v for k, v in game.round_words.items()},  # FIXED: Save round words
                    'player_roles': {str(k): v for k, v in game.player_roles.items()},  # FIXED: Save roles
                    'original_roles': {str(k): v for k, v in game.original_roles.items()},  # FIXED: Save original roles
                    'gangster_ids': list(game.gangster_ids)  # FIXED: Save gangster IDs
                }
        
        with open('livegames.json', 'w') as f:
            json.dump(games_data, f, indent=2)
        logger.info(f"Saved {len(games_data)} active games to livegames.json")
    except Exception as e:
        logger.error(f"Failed to save games to file: {e}")

def load_games_from_file():
    """Load active games from livegames.json"""
    try:
        if not os.path.exists('livegames.json'):
            logger.info("No livegames.json file found, starting fresh")
            return
        
        with open('livegames.json', 'r') as f:
            games_data = json.load(f)
        
        for chat_id_str, game_data in games_data.items():
            chat_id = int(chat_id_str)
            
            # Don't pass chat_id to GameState, set it after
            game = GameState()
            game.chat_id = chat_id
            game.players = [(uid, name) for uid, name in game_data['players']]
            game.spy_user_ids = set(game_data['spy_user_ids'])
            game.citizen_word = game_data['citizen_word']
            game.spy_word = game_data['spy_word']
            game.current_round = game_data['current_round']
            game.current_player_index = game_data['current_player_index']
            game.votes = {int(k): v for k, v in game_data['votes'].items()}
            game.eliminated_players = set(game_data['eliminated_players'])
            game.game_active = game_data['game_active']
            game.voting_active = game_data['voting_active']
            game.join_message_id = game_data.get('join_message_id')
            game.round_message_id = game_data.get('round_message_id')
            game.player_hints = {int(k): v for k, v in game_data.get('player_hints', {}).items()}
            game.spy_kill_targets = {int(k): v for k, v in game_data.get('spy_kill_targets', {}).items()}
            game.spy_kill_available = game_data.get('spy_kill_available', False)
            game.turn_time = game_data.get('turn_time', 30)
            game.discussion_time = game_data.get('discussion_time', 30)
            game.voting_time = game_data.get('voting_time', 60)
            game.delete_word_messages = game_data.get('delete_word_messages', False)
            game.dead_people_can_write = game_data.get('dead_people_can_write', True)
            
            # FIXED: Load round words
            game.round_words = {int(k): v for k, v in game_data.get('round_words', {}).items()}
            
            # FIXED: Load player roles
            game.player_roles = {int(k): v for k, v in game_data.get('player_roles', {}).items()}
            
            # FIXED: Load original roles
            game.original_roles = {int(k): v for k, v in game_data.get('original_roles', {}).items()}
            
            # FIXED: Load gangster IDs
            game.gangster_ids = game_data.get('gangster_ids', [])
            
            # FIXED: Populate word_messages dictionary from saved words and roles
            for user_id, _ in game.players:
                role = game.player_roles.get(user_id, 'Citizen')
                role_emoji = get_role_emoji(role)
                original_role = game.original_roles.get(user_id, role)
                
                if original_role == 'Spy':
                    game.word_messages[user_id] = f"""You're the Spy {role_emoji}
Your word: {game.spy_word}
Make others fool, don't let them know you're spy"""
                elif original_role == 'Gangster':
                    game.word_messages[user_id] = f"""You're {role} {role_emoji}
Your word is: {game.spy_word}"""
                else:
                    game.word_messages[user_id] = f"""You're {role} {role_emoji}
Your word is: {game.citizen_word}"""
            
            games[chat_id] = game
        
        logger.info(f"Loaded {len(games_data)} active games from livegames.json")
    except Exception as e:
        logger.error(f"Failed to load games from file: {e}")

async def periodic_save_games(context):
    """Periodically save games to file every 10 seconds"""
    while True:
        await asyncio.sleep(10)
        save_games_to_file()


async def send_game_words_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game: GameState):
    """Send summary of all game words 5 seconds after game ends"""
    await asyncio.sleep(5)
    
    # Build the message
    message = "👉 All Game words: 📃\n\nCitizens:\n\n"
    
    # Add citizen words for each round
    for round_num in sorted(game.round_words.keys()):
        citizen_word = game.round_words[round_num]["citizen"]
        message += f"Round {round_num} - {citizen_word}\n"
    
    message += "\nSpy:\n\n"
    
    # Add spy words for each round
    for round_num in sorted(game.round_words.keys()):
        spy_word = game.round_words[round_num]["spy"]
        message += f"Round {round_num} - {spy_word}\n"
    
    message += "\nJoin official group: @whosthespyofficial"
    
    try:
        # Send with blockquote formatting using HTML parse mode
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"<blockquote>{message}</blockquote>",
            parse_mode='HTML'
        )
        logger.info(f"Sent game words summary to chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send game words summary with HTML formatting: {e}")
        # Fallback to plain text if HTML fails
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e2:
            logger.error(f"Failed to send game words summary even without formatting: {e2}")



# Bot commands and game logic

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    username = update.effective_user.first_name or "Player"
    user_id = update.effective_user.id

    # CHANGE 1: Handle deep link join parameter
    if context.args and context.args[0].startswith("join_"):
        try:
            chat_id = int(context.args[0].split("_")[1])
            game = games.get(chat_id)
            
            if not game or not game.lobby_active or game.game_active:
                await update.message.reply_text("Game is not available to join right now.")
                return
            
            # Check if user already joined
            already_joined = False
            for player_id, _ in game.players:
                if player_id == user_id:
                    already_joined = True
                    break
            
            if already_joined:
                await update.message.reply_text("You've already joined the game!")
                return
            
            # Add player to game
            game.players.append((user_id, username))
            await update.message.reply_text("You've joined the game successfully 🎯")
            
            # Check if we've reached 20 players - auto start immediately
            if len(game.players) >= 20:
                game.lobby_active = False
                game.game_active = True
                game.current_round = 1
                game.current_player_index = 0

                # Randomize player order
                random.shuffle(game.players)

                # NEW: Assign roles based on player count
                assign_roles(game)

                # Assign words BEFORE sending the start message
                assign_words_for_round(game)

                # Delete lobby host message if exists
                try:
                    if game.host_message_id:
                        await context.bot.delete_message(
                            chat_id=chat_id,
                            message_id=game.host_message_id
                        )
                except:
                    pass

                # New start message format
                start_message = """🎮 Game has been started"""

                keyboard = [[InlineKeyboardButton("See the word", callback_data="check_word", api_kwargs={"style": "success"})]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                pinned_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=start_message,
                    reply_markup=reply_markup
                )
                game.start_message_id = pinned_msg.message_id

                try:
                    await context.bot.pin_chat_message(chat_id=chat_id, message_id=pinned_msg.message_id, disable_notification=True)
                except Exception as e:
                    logger.error(f"Failed to pin start message: {e}")

                # Send second message immediately with image
                second_message = """The deadly night has arrived. 💀

Trust no one, give clues but don't let the Spy know real word.

He is behind you 🔪
Only the brave ones will survive the dark night. 🌉"""
                try:
                    with open('1.jpg', 'rb') as photo:
                        await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=second_message)
                except Exception as e:
                    logger.error(f"Failed to send image 1.jpg: {e}")
                    await context.bot.send_message(chat_id=chat_id, text=second_message)

                await start_round(context, chat_id)
                
                # Send game start notification
                await notify_game_start(context, chat_id)
                return
            
            # Update lobby player list message with numbered format and hyperlinks
            players_list = "\n".join([f"{i+1}) [{name}](tg://user?id={user_id})" for i, (user_id, name) in enumerate(game.players)])

            updated_message = f"""Game started 🔪

Players : 

{players_list}

Use /begin to start the game"""

            # Keep the inline button with deep link
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username
            deep_link_url = f"https://t.me/{bot_username}?start=join_{chat_id}"
            keyboard = [[InlineKeyboardButton("JOIN GAME 🎮", url=deep_link_url, api_kwargs={"style": "success"})]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if game.host_message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game.host_message_id,
                        text=updated_message,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                except:
                    pass
            
            return
        except Exception as e:
            logger.error(f"Error handling join deep link: {e}")
            await update.message.reply_text("Failed to join the game.")
            return

    for chat_id, game in games.items():
        if game.game_active and user_id in [p[0] for p in game.players]:
            if user_id in game.spy_user_ids:
                word_message = f"""You're the Spy ☠️☠️☠️
Your word : {game.spy_word}
Make others fool, don't let them that you're spy"""
            else:
                word_message = f"""You're citizen!
Your word is : {game.citizen_word}"""
            await update.message.reply_text(word_message)
            return

    # NEW START MESSAGE
    start_message = (
        f"Hey {username}\n\n"
        "Welcome to Who's The Spy official game bot. 💀\n\n"
        "To play the game, add this bot in a group and start with /host command. /Begin to play 🕹️"
    )
    await update.message.reply_text(start_message)


async def host_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat or not update.effective_user or not update.message:
        return
    
    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return
        
    if update.effective_chat.type == 'private':
        await update.message.reply_text("Games can only be hosted in groups, not in private messages!")
        return

    chat_id = update.effective_chat.id
    game = get_or_create_game(chat_id)

    # If lobby or game is already active, send message with hyperlink to join
    if game.lobby_active or game.game_active:
        if game.lobby_active and game.host_message_id:
            # Create a deep link that points to the original host message
            try:
                chat_info = await context.bot.get_chat(chat_id)
                if chat_info.username:
                    # Public group/channel with username
                    message_link = f"https://t.me/{chat_info.username}/{game.host_message_id}"
                else:
                    # Private group - use different format
                    # Remove the -100 prefix from chat_id for the link
                    chat_id_str = str(chat_id).replace('-100', '')
                    message_link = f"https://t.me/c/{chat_id_str}/{game.host_message_id}"
                
                response_message = f"Game is active!\n[Click here to join]({message_link})"
                
                await update.message.reply_text(
                    response_message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to create message link: {e}")
                await update.message.reply_text("Game is already running!")
        else:
            await update.message.reply_text("Game is already running!")
        return

    game.lobby_active = True
    game.chat_id = chat_id
    game.admin_user_id = update.effective_user.id
    game.players = []

    # Original lobby message
    lobby_message = """Game started 🔪

Players : 

Nobody has joined yet.

Use /begin to start the game"""

    # CHANGE 1: Modified inline button to use deep link to bot's DM
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    # Create deep link that opens bot DM with start parameter
    deep_link_url = f"https://t.me/{bot_username}?start=join_{chat_id}"
    
    keyboard = [[InlineKeyboardButton("JOIN GAME 🎮", url=deep_link_url, api_kwargs={"style": "success"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_message = await update.message.reply_text(lobby_message, reply_markup=reply_markup)
    game.host_message_id = sent_message.message_id

    # Pin the lobby message automatically in the group (only first time)
    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_message.message_id)
    except Exception as e:
        logger.error(f"Failed to pin lobby message: {e}")


async def handle_join_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat or not update.effective_user:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    game = get_or_create_game(chat_id)

    # Only allow joining if lobby is active and game is not active
    if not game.lobby_active or game.game_active:
        return

    user_id = update.effective_user.id
    first_name = update.effective_user.first_name or "Player"

    # Check if user already joined
    for player_id, _ in game.players:
        if player_id == user_id:
            # Already joined, ignore without response
            return

    # Add new player
    game.players.append((user_id, first_name))

    # Send confirmation reply to join command message or callback
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text("You've joined the game!")

    # Check if we've reached 20 players - auto start immediately
    if len(game.players) >= 20:
        game.lobby_active = False
        game.game_active = True
        game.current_round = 1
        game.current_player_index = 0

        # Randomize player order
        random.shuffle(game.players)

        # NEW: Assign roles based on player count
        assign_roles(game)

        # Assign words BEFORE sending the start message
        assign_words_for_round(game)

        # Delete lobby host message if exists
        try:
            if game.host_message_id:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=game.host_message_id
                )
        except:
            pass

        # New start message format
        start_message = """🎮 Game has been started"""

        keyboard = [[InlineKeyboardButton("See the word", callback_data="check_word", api_kwargs={"style": "success"})]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        pinned_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=start_message,
            reply_markup=reply_markup
        )
        game.start_message_id = pinned_msg.message_id

        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=pinned_msg.message_id, disable_notification=True)
        except Exception as e:
            logger.error(f"Failed to pin start message: {e}")

        # Send second message immediately with image
        second_message = """The deadly night has arrived. 💀

Trust no one, give clues but don't let the Spy know real word.

He is behind you 🔪
Only the brave ones will survive the dark night. 🌉"""
        try:
            with open('1.jpg', 'rb') as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=second_message)
        except Exception as e:
            logger.error(f"Failed to send image 1.jpg: {e}")
            await context.bot.send_message(chat_id=chat_id, text=second_message)

        await start_round(context, chat_id)
        
        # Send game start notification
        await notify_game_start(context, chat_id)
        return

    # Update lobby player list message with numbered format and hyperlinks
    players_list = "\n".join([f"{i+1}) [{name}](tg://user?id={user_id})" for i, (user_id, name) in enumerate(game.players)])

    updated_message = f"""Game started 🔪

Players : 

{players_list}

Use /begin to start the game"""

    # Keep the inline button
    keyboard = [[InlineKeyboardButton("JOIN GAME 🎮", callback_data="join_game", api_kwargs={"style": "success"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if game.host_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game.host_message_id,
                text=updated_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except:
            pass

def build_game_status_message(game: GameState) -> str:
    """Build the game status message with current round, alive count and hints."""
    alive_count = len([p for p in game.players if p[0] not in game.eliminated_players])

    hints_lines = ""
    any_hints = False
    for player_id, player_name in game.players:
        if player_id in game.eliminated_players:
            continue
        player_hints_list = game.player_hints.get(player_id, [])
        if player_hints_list:
            any_hints = True
            last_hint = player_hints_list[-1]
            hints_lines += f"🔹 {player_name}:  \n\"{last_hint}\"\n\n"

    if not any_hints:
        hints_section = "🔹 No hints yet\n"
    else:
        hints_section = hints_lines

    message = (
        "🕵️‍♂️ GAME STARTED!\n\n"
        "Spy is wandering around the city...\n"
        "Be careful ⚠️\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"🎯 𝗥𝗼𝘂𝗻𝗱 - {game.current_round}  \n"
        f"👥 𝗣𝗹𝗮𝘆𝗲𝗿𝘀 𝗔𝗹𝗶𝘃𝗲 - {alive_count}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "💬 𝗛𝗶𝗻𝘁𝘀:\n\n"
        f"{hints_section}"
        "━━━━━━━━━━━━━━━\n\n"
        "⌨️ Type /hint (your hint)"
    )
    return message


async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    if update.effective_chat.type == 'private':
        await update.message.reply_text("Use /hint in the game group chat!")
        return

    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.first_name or "Player"
    game = games.get(chat_id)

    if not game or not game.game_active:
        await update.message.reply_text("No active game right now!")
        return

    if user_id not in [p[0] for p in game.players]:
        await update.message.reply_text("You are not in this game!")
        return

    if user_id in game.eliminated_players:
        await update.message.reply_text(f"{username} You are dead, and can't give hints! 🦄")
        return

    if game.discussion_active:
        await update.message.reply_text(f"{username} you can't give hints now")
        return

    if not context.args:
        await update.message.reply_text("Please provide your hint: /hint (your hint here)")
        return

    if game.player_hints.get(user_id):
        spam_key = (chat_id, user_id)
        now = time.time()
        last_sent = _hint_spam_cooldown.get(spam_key, 0)
        if now - last_sent >= 30:
            _hint_spam_cooldown[spam_key] = now
            await update.message.reply_text(f"💭 {username} You have already given your hint")
        return

    hint_text = " ".join(context.args)

    if user_id not in game.player_hints:
        game.player_hints[user_id] = []
    game.player_hints[user_id].append(hint_text)

    status_message = build_game_status_message(game)

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    bot_dm_link = f"https://t.me/{bot_username}"

    game_keyboard = [
        [
            InlineKeyboardButton("Check the word", callback_data="check_word", api_kwargs={"style": "success"}),
            InlineKeyboardButton("Go to bot", url=bot_dm_link, api_kwargs={"style": "primary"})
        ]
    ]
    game_reply_markup = InlineKeyboardMarkup(game_keyboard)

    # Delete the previous hint status message if it exists
    if game.last_hint_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=game.last_hint_message_id)
        except Exception as e:
            logger.error(f"Failed to delete previous hint message: {e}")
        game.last_hint_message_id = None

    try:
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=status_message,
            reply_markup=game_reply_markup
        )
        game.last_hint_message_id = sent_msg.message_id
    except Exception as e:
        logger.error(f"Failed to send hint status message: {e}")


async def send_spy_team_introduction(context: ContextTypes.DEFAULT_TYPE, game: GameState):
    """Send spy team introduction in first round only"""
    try:
        # Only send in first round
        if game.current_round != 1:
            return
        
        # Get all spy team members
        spy_id = game.spy_user_ids[0] if game.spy_user_ids else None
        
        if not spy_id:
            return
        
        spy_name = next((name for uid, name in game.players if uid == spy_id), "Unknown")
        
        # Get gangster info
        gangster_info = []
        for gangster_id in game.gangster_ids:
            gangster_name = next((name for uid, name in game.players if uid == gangster_id), "Unknown")
            gangster_info.append((gangster_id, gangster_name))
        
        # Send to Spy
        spy_message = "You're Spy - The main villain 🕶\n\nYour teammates:\n\n"
        
        if gangster_info:
            for gid, gname in gangster_info:
                spy_message += f"{gname} - Gangster 🥷\n"
        else:
            spy_message += "No gangsters in this game"
        
        try:
            await context.bot.send_message(
                chat_id=spy_id,
                text=spy_message
            )
            logger.info(f"Spy team introduction sent to spy {spy_id}")
        except Exception as e:
            logger.error(f"Failed to send spy team intro to spy: {e}")
        
        # Send to each Gangster
        for gangster_id, gangster_name in gangster_info:
            gangster_message = "You're gangster 🥷\n\nYour teammates:\n\n"
            gangster_message += f"{spy_name} - Spy 🕶\n"
            
            # Add other gangsters
            for gid, gname in gangster_info:
                if gid != gangster_id:
                    gangster_message += f"{gname} - Gangster 🥷\n"
            
            try:
                await context.bot.send_message(
                    chat_id=gangster_id,
                    text=gangster_message
                )
                logger.info(f"Spy team introduction sent to gangster {gangster_id}")
            except Exception as e:
                logger.error(f"Failed to send spy team intro to gangster {gangster_id}: {e}")
        
    except Exception as e:
        logger.error(f"Failed to send spy team introduction: {e}")

async def promote_new_spy(context: ContextTypes.DEFAULT_TYPE, game: GameState, chat_id: int):
    """Promote a gangster to spy when spy is killed"""
    try:
        # Check if spy is dead and gangsters are alive
        spy_id = game.spy_user_ids[0] if game.spy_user_ids else None
        
        if not spy_id or spy_id not in game.eliminated_players:
            return  # Spy is still alive
        
        # Get alive gangsters
        alive_gangsters = [gid for gid in game.gangster_ids if gid not in game.eliminated_players]
        
        if not alive_gangsters:
            return  # No gangsters to promote
        
        # Randomly select new spy from alive gangsters
        new_spy_id = random.choice(alive_gangsters)
        new_spy_name = next((name for uid, name in game.players if uid == new_spy_id), "Unknown")
        
        # Remove from gangster list and add to spy list
        game.gangster_ids.remove(new_spy_id)
        game.spy_user_ids = [new_spy_id]
        
        # Update CURRENT role to Spy, but keep original role as Gangster
        game.player_roles[new_spy_id] = 'Spy'
        # DON'T update game.original_roles - it stays as 'Gangster'
        
        # Notify all spy team members
        spy_team_members = [new_spy_id] + [gid for gid in game.gangster_ids if gid not in game.eliminated_players]
        
        promotion_message = f"Your leader was killed\n\n{new_spy_name} is the new Spy"
        
        for member_id in spy_team_members:
            try:
                await context.bot.send_message(
                    chat_id=member_id,
                    text=promotion_message
                )
            except Exception as e:
                logger.error(f"Failed to send promotion message to {member_id}: {e}")
        
        logger.info(f"Promoted gangster {new_spy_id} to spy")
        
        # CRITICAL FIX: ALWAYS send kill menu to new spy (not just if spy_kill_available)
        # This ensures promoted spy gets kill button even if promoted mid-round
        
        # Get alive non-spy-team players
        alive_players = [(user_id, name) for user_id, name in game.players
                         if user_id not in game.eliminated_players 
                         and user_id not in game.spy_user_ids 
                         and user_id not in game.gangster_ids]

        if alive_players:
            kill_message = "Select whom you want to kill:"

            keyboard = []
            for user_id, name in alive_players:
                keyboard.append([InlineKeyboardButton(name, callback_data=f"kill_{user_id}")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                msg = await context.bot.send_message(
                    chat_id=new_spy_id,
                    text=kill_message,
                    reply_markup=reply_markup
                )
                # Track message ID for deletion later
                game.spy_kill_message_ids[new_spy_id] = msg.message_id
                logger.info(f"Kill menu sent to new spy {new_spy_id} immediately after promotion")
            except Exception as e:
                logger.error(f"Failed to send kill menu to new spy: {e}")
        
    except Exception as e:
        logger.error(f"Failed to promote new spy: {e}")



# CHANGE 4: Add /alive command
async def alive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat:
        return

    # Check if user is banned
    if update.effective_user and is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    chat_id = update.effective_chat.id
    game = get_or_create_game(chat_id)

    if not game.game_active:
        await update.message.reply_text("No game is currently active!")
        return

    alive_players = [p for p in game.players if p[0] not in game.eliminated_players]
    
    if not alive_players:
        await update.message.reply_text("No players are alive!")
        return

    # Build the alive players list
    alive_list = "👥 Alive Players:\n\n"
    for idx, (player_id, player_name) in enumerate(alive_players, 1):
        alive_list += f"{idx}. {player_name}\n"
    
    # Get unique roles that are still alive
    alive_roles = set()
    for player_id, player_name in alive_players:
        role = game.player_roles.get(player_id, "Citizen")
        alive_roles.add(role)
    
    # Map roles to emojis
    role_emoji_map = {
        "Citizen": "👤",
        "Detective": "🕵",
        "Spy": "🕶️",
        "Gangster": "🥷",
        "Doctor": "👨‍⚕️",
        "Kamikaze": "💣",
        "Hacker": "💻"
    }
    
    # Build roles alive string (only unique roles)
    roles_display = []
    for role in ["Citizen", "Detective", "Doctor", "Spy", "Gangster", "Kamikaze", "Hacker"]:
        if role in alive_roles:
            emoji = role_emoji_map.get(role, "")
            roles_display.append(f"{role} {emoji}")
    
    roles_alive_text = ", ".join(roles_display)
    
    # Add roles and total
    alive_list += f"\nRoles alive: {roles_alive_text}\n\n"
    alive_list += f"Total: {len(alive_players)} people"

    await update.message.reply_text(alive_list)

# Add message deletion for eliminated players
async def turn_text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        except:
            pass
        return

    # NEW: Handle private messages for last words AND spy team communication
    if update.effective_chat.type == 'private':
        user_id = update.effective_user.id
        message_text = update.message.text
        
        # First check if it's last words
        user_game = None
        user_chat_id = None
        
        for chat_id, game in games.items():
            if game.game_active and user_id in game.last_words_eligible:
                user_game = game
                user_chat_id = chat_id
                await handle_last_words(update, context)
                return
        
        # Then check if it's spy team communication
        for chat_id, game in games.items():
            if game.game_active and (user_id in game.spy_user_ids or user_id in game.gangster_ids) and user_id not in game.eliminated_players:
                # Check if abilities are available (from first turn to discussion)
                if game.spy_kill_available:
                    # This is spy team communication
                    sender_name = next((name for uid, name in game.players if uid == user_id), "Unknown")
                    sender_role = game.original_roles.get(user_id, 'Spy')
                    sender_emoji = get_role_emoji(sender_role)
                    
                    # Format: "Role emoji - Name: message"
                    team_message = f"{sender_role} {sender_emoji} - {sender_name}: {message_text}"
                    
                    # Send to all spy team members
                    spy_team = []
                    
                    # Add spy if alive
                    for spy_id in game.spy_user_ids:
                        if spy_id not in game.eliminated_players and spy_id != user_id:  # Don't send to sender
                            spy_team.append(spy_id)
                    
                    # Add gangsters if alive
                    for gangster_id in game.gangster_ids:
                        if gangster_id not in game.eliminated_players and gangster_id != user_id:  # Don't send to sender
                            spy_team.append(gangster_id)
                    
                    # Send to all team members
                    for member_id in spy_team:
                        try:
                            await context.bot.send_message(
                                chat_id=member_id,
                                text=team_message
                            )
                        except Exception as e:
                            logger.error(f"Failed to send spy team message to {member_id}: {e}")
                    
                    # Confirm to sender
                    await update.message.reply_text("Message sent to your team")
                    return
        
        # If not last words or spy team communication, ignore
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_text = update.message.text
    if not message_text:
        return
    game = games.get(chat_id)
    if not game or not game.game_active:
        return

    # Check if user is eliminated and implement dead people message deletion
    if user_id in game.eliminated_players:
        if not game.dead_people_can_write:
            # Delete the message if dead people can't write
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
                logger.info(f"Deleted message from eliminated player {user_id}")
            except Exception as e:
                logger.error(f"Failed to delete message from eliminated player: {e}")
            return
        else:
            return  # Let them talk but don't process their hints

    # Check if message contains game words and implement word deletion
    if game.delete_word_messages:
        message_lower = message_text.lower()
        citizen_word_lower = game.citizen_word.lower()
        spy_word_lower = game.spy_word.lower()
        
        if citizen_word_lower in message_lower or spy_word_lower in message_lower:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
                logger.info(f"Deleted message containing game word from user {user_id}")
            except Exception as e:
                logger.error(f"Failed to delete message containing game word: {e}")
            return

    # Only /hint command counts as a hint — plain messages are ignored

async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    contact_message = (
        "Bot creator info:\n\n"
        "Kindly dm @userunknown1bot\n"
        "To contact us"
    )
    
    await update.message.reply_text(contact_message)

async def begin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat or not update.message:
        return

    # Check if user is banned
    if update.effective_user and is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    if update.effective_chat.type == 'private':
        await update.message.reply_text("Games can only be started in groups, not in private messages!")
        return

    chat_id = update.effective_chat.id
    game = get_or_create_game(chat_id)

    # Minimum 4 players required to start, maximum 20
    if not game.lobby_active or game.game_active or len(game.players) < 4:
        if len(game.players) < 4:
            await update.message.reply_text("Need at least 4 players to start the game. Please wait for more players to join.")
        return

    game.lobby_active = False
    game.game_active = True
    game.current_round = 1
    game.current_player_index = 0

    # Randomize player order
    random.shuffle(game.players)

    # Assign roles based on player count
    assign_roles(game)

    # Set turn time based on player count
    player_count = len(game.players)
    if 4 <= player_count <= 6:
        game.turn_time = 120
    else:
        game.turn_time = 180

    # Send role introduction messages to all players IMMEDIATELY after role assignment
    await send_role_introduction_messages(context, game)

    # Assign words BEFORE sending the start message
    assign_words_for_round(game)

    # Save game state to file after game starts
    save_games_to_file()

    # Delete lobby host message if exists
    try:
        if game.host_message_id:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=game.host_message_id
            )
    except:
        pass

    # Send countdown message immediately
    await context.bot.send_message(chat_id=chat_id, text="⏳ Game will be starting in 10 seconds")

    # Wait 10 seconds
    await asyncio.sleep(10)

    # Build the GAME STARTED message
    alive_count = len([p for p in game.players if p[0] not in game.eliminated_players])
    game_started_text = (
        "🕵️‍♂️ GAME STARTED!\n\n"
        "Spy is wandering around the city...\n"
        "Be careful ⚠️\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"🎯 𝗥𝗼𝘂𝗻𝗱 - {game.current_round}  \n"
        f"👥 𝗣𝗹𝗮𝘆𝗲𝗿𝘀 𝗔𝗹𝗶𝘃𝗲 - {alive_count}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "💬 𝗛𝗶𝗻𝘁𝘀:\n\n"
        "🔹 No hints yet\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⌨️ Type /hint (your hint)"
    )

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    bot_dm_link = f"https://t.me/{bot_username}"

    game_keyboard = [
        [
            InlineKeyboardButton("Check the word", callback_data="check_word", api_kwargs={"style": "success"}),
            InlineKeyboardButton("Go to bot", url=bot_dm_link, api_kwargs={"style": "primary"})
        ]
    ]
    game_reply_markup = InlineKeyboardMarkup(game_keyboard)

    pinned_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=game_started_text,
        reply_markup=game_reply_markup
    )
    game.start_message_id = pinned_msg.message_id
    game.round_message_id = pinned_msg.message_id

    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=pinned_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Failed to pin start message: {e}")

    # Start round 1 immediately
    await start_round(context, chat_id)
    
    # Send game start notification
    await notify_game_start(context, chat_id)

async def execute_detective_kill(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Execute Detective kill if they selected a target - returns tuple (killed_target_id, detective_was_killed_by_spy)"""
    game = games.get(chat_id)
    if not game or not game.detective_kill_target:
        return None, False
    
    target_id = game.detective_kill_target
    
    # Check if target is still alive
    if target_id in game.eliminated_players:
        game.detective_kill_target = None
        return None, False
    
    # Check if Doctor saved this target
    if game.doctor_save_target == target_id:
        logger.info(f"Doctor saved {target_id} from Detective kill")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Someone was cured by the doctor tonight 💉"
        )
        game.detective_kill_target = None
        return None, False
    
    # Check if detective is being killed by spy
    detective_killed_by_spy = False
    for spy_id in game.spy_user_ids:
        if spy_id in game.eliminated_players:
            continue
        spy_target = game.spy_kill_targets.get(spy_id)
        if spy_target == game.detective_id:
            detective_killed_by_spy = True
            break
    
    # Mark target as killed
    game.eliminated_players.add(target_id)
    target_name = next((name for uid, name in game.players if uid == target_id), "Unknown")
    
    # Mark as eligible for last words
    import time
    game.last_words_eligible[target_id] = time.time()
    
    # Send notification to killed player
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"You were killed by the Detective! 🕵\n\nYou have 60 seconds to send your last words. Just type your message here and it will be sent to the game chat."
        )
    except Exception as e:
        logger.error(f"Failed to send last words notification to {target_id}: {e}")
    
    # Send kill announcement to chat with role
    target_role = game.player_roles.get(target_id, 'Citizen')
    target_role_emoji = get_role_emoji(target_role)
    
    kill_announcement = f"{target_role} {target_role_emoji} - [{target_name}](tg://user?id={target_id}) was killed by Detective 🕵"
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=kill_announcement,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send detective kill announcement: {e}")
    
    # NEW: Check if target was Kamikaze - kill detective too
    if target_id == game.kamikaze_id:
        await asyncio.sleep(1)  # Small delay
        
        # Kill detective
        game.eliminated_players.add(game.detective_id)
        
        detective_name = next((name for uid, name in game.players if uid == game.detective_id), "Unknown")
        detective_role = game.original_roles.get(game.detective_id, 'Detective')
        detective_emoji = get_role_emoji(detective_role)
        
        # Mark detective for last words
        game.last_words_eligible[game.detective_id] = time.time()
        
        try:
            await context.bot.send_message(
                chat_id=game.detective_id,
                text=f"Kamikaze killed you back! 💣\n\nYou have 60 seconds to send your last words."
            )
        except Exception as e:
            logger.error(f"Failed to send last words to detective: {e}")
        
        # Send kamikaze revenge announcement
        kamikaze_revenge = f"{detective_role} {detective_emoji} - [{detective_name}](tg://user?id={game.detective_id}) was killed by Kamikaze 💣"
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=kamikaze_revenge,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send kamikaze revenge: {e}")
    
    # Reset detective kill target
    game.detective_kill_target = None
    
    return target_id, detective_killed_by_spy

async def delayed_kamikaze_revenge(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game: GameState):
    """Execute kamikaze revenge after 30 seconds"""
    await asyncio.sleep(30)
    
    # Execute the revenge kill
    await execute_kamikaze_revenge(context, chat_id, game)


async def start_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_or_create_game(chat_id)

    # Clear all hints at the start of each round
    game.player_hints = {}
    game.discussion_active = False
    game.last_hint_message_id = None
    logger.info(f"Cleared hints at start of round {game.current_round}")

    # Send spy team introduction in round 1 only (FIRST - before anything else)
    if game.current_round == 1:
        await send_spy_team_introduction(context, game)

    # Enable and send abilities at the start of EVERY round
    game.spy_kill_available = True
    game.spy_kill_targets = {}

    game.detective_action_available = True
    game.detective_inspect_target = None
    game.detective_kill_target = None

    game.doctor_save_target = None

    # Send spy kill DM to Spy and all Gangsters
    await send_spy_kill_dm(context, game)
    logger.info(f"Spy kill DM sent at start of round {game.current_round}")

    # Send Detective action menu
    await send_detective_action_menu(context, game)
    logger.info(f"Detective action menu sent at start of round {game.current_round}")

    # Send Doctor save menu
    await send_doctor_save_menu(context, game)
    logger.info(f"Doctor save menu sent at start of round {game.current_round}")

    game.round_message_id = None

    asyncio.create_task(round_timer(context, chat_id, game.current_round))


async def execute_spy_kill(context: ContextTypes.DEFAULT_TYPE, chat_id: int, detective_target_id=None, detective_killed_by_spy=False):
    """Execute spy kill and announce result - handles mutual kills with detective"""
    game = get_or_create_game(chat_id)

    # Disable spy kill availability
    game.spy_kill_available = False

    killed_players = []
    detective_also_killed = False
    doctor_saved_someone = False
    
    # Get spy's choice (prioritize main spy's choice)
    spy_id = game.spy_user_ids[0] if game.spy_user_ids else None
    spy_target = None
    
    # Check if main spy made a choice (even if killed this round - allow revenge)
    if spy_id:
        spy_target = game.spy_kill_targets.get(spy_id)
    
    # If main spy didn't make a choice, check gangsters
    if not spy_target:
        for gangster_id in game.gangster_ids:
            if gangster_id in game.eliminated_players:
                continue
            target_id = game.spy_kill_targets.get(gangster_id)
            if target_id:
                spy_target = target_id
                break  # Take first gangster's choice
    
    # Process the kill if there's a target
    if spy_target:
        # Check if Doctor saved this target
        if game.doctor_save_target == spy_target:
            logger.info(f"Doctor saved {spy_target} from Spy kill")
            doctor_saved_someone = True
        else:
            # Check if this is mutual kill with detective
            if detective_target_id == spy_id and spy_target == game.detective_id:
                # Mutual kill - detective also dies
                if game.detective_id not in game.eliminated_players:
                    # Check if doctor saved detective
                    if game.doctor_save_target == game.detective_id:
                        logger.info(f"Doctor saved Detective from Spy revenge kill")
                        doctor_saved_someone = True
                    else:
                        killed_players.append(game.detective_id)
                        game.eliminated_players.add(game.detective_id)
                        detective_also_killed = True
            # Normal kill
            elif spy_target not in game.eliminated_players:
                killed_players.append(spy_target)
                game.eliminated_players.add(spy_target)

    # Mark killed players as eligible for last words
    import time
    current_timestamp = time.time()
    for killed_id in killed_players:
        game.last_words_eligible[killed_id] = current_timestamp
        # Send DM to killed player about last words
        try:
            killed_name = next(name for user_id, name in game.players if user_id == killed_id)
            await context.bot.send_message(
                chat_id=killed_id,
                text=f"You were killed by the Spy! 🔪\n\nYou have 60 seconds to send your last words. Just type your message here and it will be sent to the game chat."
            )
        except Exception as e:
            logger.error(f"Failed to send last words notification to {killed_id}: {e}")

    # Check if spy was killed and promote new spy
    if spy_id and spy_id in game.eliminated_players:
        await promote_new_spy(context, game, chat_id)

    # Send doctor save message if doctor saved someone
    if doctor_saved_someone:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Someone was cured by the doctor tonight 💉"
        )

    if killed_players:
        # Someone was killed
        if len(killed_players) == 1:
            killed_id = killed_players[0]
            killed_name = next(name for user_id, name in game.players if user_id == killed_id)
            killed_role = game.player_roles.get(killed_id, 'Citizen')
            killed_role_emoji = get_role_emoji(killed_role)
            
            if detective_also_killed:
                kill_announcement = (
                    f"🔪💥 {killed_role} {killed_role_emoji} - [{killed_name}](tg://user?id={killed_id}) (Detective) was also killed by The spy\n\n"
                    f"Both Detective and Spy killed each other in the night!"
                )
            else:
                kill_announcement = (
                    f"{killed_role} {killed_role_emoji} - [{killed_name}](tg://user?id={killed_id}) was killed by The Spy\n\n"
                    f"Citizens heard [{killed_name}](tg://user?id={killed_id}) screaming 'Whaaa—aaaat?! Nooooo—oo—ooo! Please Don't Kill me, I'm innocent'"
                )
        else:
            # Multiple kills - create hyperlinks for all killed players with roles
            killed_info = []
            for kid in killed_players:
                kname = next(name for user_id, name in game.players if user_id == kid)
                krole = game.player_roles.get(kid, 'Citizen')
                krole_emoji = get_role_emoji(krole)
                killed_info.append(f"{krole} {krole_emoji} - [{kname}](tg://user?id={kid})")
            
            if detective_also_killed:
                kill_announcement = (
                    f"🔪💥 {', '.join(killed_info)} were killed\n\n"
                    f"Both Detective and Spy killed each other in the night!"
                )
            else:
                kill_announcement = (
                    f"{', '.join(killed_info)} were killed by The Spy\n\n"
                    f"Citizens heard them screaming 'Whaaa—aaaat?! Nooooo—oo—ooo! Please Don't Kill me, I'm innocent'"
                )
        
        # Send kill announcement with image 102.jpg
        try:
            with open('102.jpg', 'rb') as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=kill_announcement, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send image 102.jpg: {e}")
            await context.bot.send_message(chat_id=chat_id, text=kill_announcement, parse_mode='Markdown')
        
        # Check if any killed player was Kamikaze - kill the spy/gangster who killed them
        kamikaze_killed_by_spy_team = None
        for killed_id in killed_players:
            if killed_id == game.kamikaze_id:
                # Find who killed kamikaze
                for spy_team_member in game.spy_user_ids:
                    if spy_team_member in game.eliminated_players:
                        continue
                    if game.spy_kill_targets.get(spy_team_member) == game.kamikaze_id:
                        kamikaze_killed_by_spy_team = spy_team_member
                        break
                break
        
        # Execute kamikaze revenge on spy team member
        if kamikaze_killed_by_spy_team:
            await asyncio.sleep(1)  # Small delay
            
            # Kill the spy team member
            game.eliminated_players.add(kamikaze_killed_by_spy_team)
            
            killer_name = next((name for uid, name in game.players if uid == kamikaze_killed_by_spy_team), "Unknown")
            killer_role = game.original_roles.get(kamikaze_killed_by_spy_team, 'Spy')
            killer_emoji = get_role_emoji(killer_role)
            
            # Mark for last words
            game.last_words_eligible[kamikaze_killed_by_spy_team] = current_timestamp
            
            try:
                await context.bot.send_message(
                    chat_id=kamikaze_killed_by_spy_team,
                    text=f"Kamikaze killed you back! 💣\n\nYou have 60 seconds to send your last words."
                )
            except Exception as e:
                logger.error(f"Failed to send last words to spy team member: {e}")
            
            # Send kamikaze revenge announcement
            kamikaze_revenge = f"{killer_role} {killer_emoji} - [{killer_name}](tg://user?id={kamikaze_killed_by_spy_team}) was killed by Kamikaze 💣"
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=kamikaze_revenge,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to send kamikaze revenge: {e}")
    else:
        # No one was killed
        if not doctor_saved_someone:
            await context.bot.send_message(
                chat_id=chat_id,
                text="No one was killed tonight."
            )

    # Reset spy kill targets
    game.spy_kill_targets = {}

async def execute_kamikaze_revenge(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game: GameState):
    """Execute Kamikaze's revenge kill after they selected a target"""
    if not game.kamikaze_revenge_target:
        return
    
    target_id = game.kamikaze_revenge_target
    
    # Check if target is still alive
    if target_id in game.eliminated_players:
        game.kamikaze_revenge_target = None
        return
    
    # Kill the target
    game.eliminated_players.add(target_id)
    
    # Get target info
    target_name = next((name for uid, name in game.players if uid == target_id), "Unknown")
    target_role = game.player_roles.get(target_id, 'Citizen')
    target_emoji = get_role_emoji(target_role)
    
    # Mark as eligible for last words
    import time
    game.last_words_eligible[target_id] = time.time()
    
    # Send last words notification
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"You were killed by Kamikaze! 💣\n\nYou have 60 seconds to send your last words. Just type your message here and it will be sent to the game chat."
        )
    except Exception as e:
        logger.error(f"Failed to send last words to kamikaze victim: {e}")
    
    # Send kill announcement
    kamikaze_kill_announcement = f"{target_role} {target_emoji} - [{target_name}](tg://user?id={target_id}) was killed by Kamikaze 💣"
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=kamikaze_kill_announcement,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send kamikaze revenge announcement: {e}")
    
    # Reset kamikaze revenge
    game.kamikaze_revenge_target = None
    game.kamikaze_revenge_timestamp = None

def assign_roles(game):
    """Assign roles to players based on player count"""
    player_count = len(game.players)
    alive_players = [p[0] for p in game.players if p[0] not in game.eliminated_players]
    
    # Reset all role assignments
    game.player_roles = {}
    game.spy_user_ids = []
    game.detective_id = None
    game.doctor_id = None
    game.kamikaze_id = None
    game.hacker_id = None
    game.gangster_ids = []
    
    # Shuffle for random assignment
    available_players = alive_players.copy()
    random.shuffle(available_players)
    
    # Role assignment based on player count
    role_config = {
        4: {'citizens': 3, 'spy': 1, 'detective': 0, 'doctor': 0, 'gangster': 0, 'kamikaze': 0, 'hacker': 0},
        5: {'citizens': 3, 'spy': 1, 'detective': 1, 'doctor': 0, 'gangster': 0, 'kamikaze': 0, 'hacker': 0},
        6: {'citizens': 3, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 0, 'kamikaze': 0, 'hacker': 0},
        7: {'citizens': 3, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 1, 'kamikaze': 0, 'hacker': 0},
        8: {'citizens': 4, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 1, 'kamikaze': 0, 'hacker': 0},
        9: {'citizens': 4, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 1, 'kamikaze': 1, 'hacker': 0},
        10: {'citizens': 4, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 2, 'kamikaze': 1, 'hacker': 0},
        11: {'citizens': 4, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 2, 'kamikaze': 1, 'hacker': 1},
        12: {'citizens': 4, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        13: {'citizens': 5, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        14: {'citizens': 6, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        15: {'citizens': 7, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        16: {'citizens': 8, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        17: {'citizens': 9, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        18: {'citizens': 10, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        19: {'citizens': 11, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
        20: {'citizens': 12, 'spy': 1, 'detective': 1, 'doctor': 1, 'gangster': 3, 'kamikaze': 1, 'hacker': 1},
    }
    
    config = role_config.get(player_count, {'citizens': player_count - 1, 'spy': 1, 'detective': 0, 'doctor': 0, 'gangster': 0, 'kamikaze': 0, 'hacker': 0})
    
    idx = 0
    
    # Assign Spy (always 1)
    if config['spy'] > 0 and idx < len(available_players):
        spy_id = available_players[idx]
        game.spy_user_ids.append(spy_id)
        game.player_roles[spy_id] = 'Spy'
        idx += 1
    
    # Assign Detective
    if config['detective'] > 0 and idx < len(available_players):
        detective_id = available_players[idx]
        game.detective_id = detective_id
        game.player_roles[detective_id] = 'Detective'
        idx += 1
    
    # Assign Doctor
    if config['doctor'] > 0 and idx < len(available_players):
        doctor_id = available_players[idx]
        game.doctor_id = doctor_id
        game.player_roles[doctor_id] = 'Doctor'
        idx += 1
    
    # Assign Gangsters
    for _ in range(config['gangster']):
        if idx < len(available_players):
            gangster_id = available_players[idx]
            game.gangster_ids.append(gangster_id)
            game.spy_user_ids.append(gangster_id)  # Gangsters are part of spy team
            game.player_roles[gangster_id] = 'Gangster'
            idx += 1
    
    # Assign Kamikaze
    if config['kamikaze'] > 0 and idx < len(available_players):
        kamikaze_id = available_players[idx]
        game.kamikaze_id = kamikaze_id
        game.player_roles[kamikaze_id] = 'Kamikaze'
        idx += 1
    
    # Assign Hacker
    if config['hacker'] > 0 and idx < len(available_players):
        hacker_id = available_players[idx]
        game.hacker_id = hacker_id
        game.player_roles[hacker_id] = 'Hacker'
        idx += 1
    
    # Assign remaining as Citizens
    for i in range(idx, len(available_players)):
        citizen_id = available_players[i]
        game.player_roles[citizen_id] = 'Citizen'
    
    # Store original roles for end game display
    game.original_roles = game.player_roles.copy()
    
    logger.info(f"Roles assigned: {game.player_roles}")


async def round_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, round_num: int):
    game = games.get(chat_id)
    if not game:
        return

    turn_time = game.turn_time
    await asyncio.sleep(turn_time)

    # Check if game still exists and is active
    game = games.get(chat_id)
    if not game or not game.game_active:
        return

    # Check if round is still the same
    if game.current_round != round_num:
        return

    # Round time is up — move straight to the kill and voting phase
    await start_voting(context, chat_id)

async def send_spy_kill_dm(context: ContextTypes.DEFAULT_TYPE, game: GameState):
    """Send kill selection menu to Spy and Gangsters"""
    try:
        # Get alive non-spy-team players
        alive_players = [(user_id, name) for user_id, name in game.players
                         if user_id not in game.eliminated_players 
                         and user_id not in game.spy_user_ids]

        if not alive_players:
            logger.info("No targets available for spy kill")
            return

        kill_message = "Select whom you want to kill:"

        keyboard = []
        for user_id, name in alive_players:
            keyboard.append([InlineKeyboardButton(name, callback_data=f"kill_{user_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        # FIXED: Send to ALL spy team members (spy_user_ids already includes gangsters)
        for spy_team_member in game.spy_user_ids:
            if spy_team_member in game.eliminated_players:
                continue
            try:
                msg = await context.bot.send_message(
                    chat_id=spy_team_member,
                    text=kill_message,
                    reply_markup=reply_markup
                )
                # Track message ID for deletion later
                game.spy_kill_message_ids[spy_team_member] = msg.message_id
                logger.info(f"Kill menu sent to spy team member {spy_team_member}")
            except Exception as e:
                logger.error(f"Failed to send kill menu to spy team member {spy_team_member}: {e}")

    except Exception as e:
        logger.error(f"Failed to send spy kill DM: {e}")



# CHANGE 5: Modified kill notification
async def handle_spy_kill_phase(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_or_create_game(chat_id)

    # CHANGE 2: New multiple spy kill logic
    killed_players = []
    if len(game.spy_user_ids) >= 2:
        # Count votes for each target
        kill_votes = {}
        active_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
        
        for spy_id in active_spies:
            target_id = game.spy_kill_targets.get(spy_id)
            if target_id and target_id not in game.eliminated_players:
                kill_votes[target_id] = kill_votes.get(target_id, 0) + 1
        
        if kill_votes:
            # Find targets with 2 or more votes
            consensus_kills = [target_id for target_id, votes in kill_votes.items() if votes >= 2]
            
            if consensus_kills:
                # Kill all consensus targets
                for target_id in consensus_kills:
                    killed_players.append(target_id)
                    game.eliminated_players.add(target_id)
            else:
                # No consensus, kill the first spy's choice only
                first_spy_choice = None
                for spy_id in active_spies:
                    target_id = game.spy_kill_targets.get(spy_id)
                    if target_id and target_id not in game.eliminated_players:
                        first_spy_choice = target_id
                        break
                
                if first_spy_choice:
                    killed_players.append(first_spy_choice)
                    game.eliminated_players.add(first_spy_choice)
    else:
        # Single spy logic (original)
        for spy_id in game.spy_user_ids:
            if spy_id in game.eliminated_players:
                continue
            target_id = game.spy_kill_targets.get(spy_id)
            if target_id and target_id not in game.eliminated_players:
                killed_players.append(target_id)
                game.eliminated_players.add(target_id)

    if killed_players:
        # Someone was killed
        if len(killed_players) == 1:
            killed_name = next(name for user_id, name in game.players if user_id == killed_players[0])
            kill_announcement = (
                f"While everybody was sleeping, {killed_name} was killed by The spy 🔪\n\n"
                f"Citizens heard {killed_name} screaming, \"Whaa—aaa—aaat? Noo—o—oooo! Please Don't Kill me\" 🔪😢"
            )
        else:
            killed_names = [next(name for user_id, name in game.players if user_id == kid) for kid in killed_players]
            kill_announcement = (
                f"While everybody was sleeping, {', '.join(killed_names)} were killed by The spies 🔪\n\n"
                f"Citizens heard them screaming, \"Whaa—aaa—aaat? Noo—o—oooo! Please Don't Kill me\" 🔪😢"
            )
        
        # Send kill announcement with image
        try:
            with open('kill.jpg', 'rb') as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=kill_announcement)
        except Exception as e:
            logger.error(f"Failed to send image kill.jpg: {e}")
            await context.bot.send_message(chat_id=chat_id, text=kill_announcement)
    else:
        # No one was killed
        kill_announcement = (
            "Spy is on holiday! 🗿\n\n"
            "Nobody gets killed today"
        )
        
        # Send no kill announcement with image
        try:
            with open('nokill.jpg', 'rb') as photo:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=kill_announcement)
        except Exception as e:
            logger.error(f"Failed to send image nokill.jpg: {e}")
            await context.bot.send_message(chat_id=chat_id, text=kill_announcement)

    # Check win conditions
    alive_players = [(user_id, name) for user_id, name in game.players if user_id not in game.eliminated_players]
    alive_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
    alive_citizens = [p for p in alive_players if p[0] not in game.spy_user_ids]

    if len(alive_spies) >= len(alive_citizens):
        # Spies win
        coins_reward = 50
        for spy_id in game.spy_user_ids:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=coins_reward)

        # CHANGE 4: Include ALL players in winning message
        all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
        
        for uid, name in all_citizens:
            update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

        spy_winners_list = "\n".join([f"[{next(name for user_id, name in game.players if user_id == spy_id)}](tg://user?id={spy_id}) - Spy" for spy_id in alive_spies])
        
        # Losers are all citizens (dead and alive)
        losers_list = "\n".join([f"[{name}](tg://user?id={uid}) - Citizen" for uid, name in all_citizens])

        game_end_message = (
            f"🎉 Spy won! 🎉\n\n"
            f"Winners :\n\n"
            f"{spy_winners_list}\n\n"
            f"Losers :\n\n"
            f"{losers_list}"
        )

        await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
        
        # NEW: Send words summary after 5 seconds
        asyncio.create_task(send_game_words_summary(context, chat_id, game))
        
        remove_game(chat_id)
        return

    # CHANGE 6: New discussion message format
    game.discussion_active = True
    discussion_message = (
        f"The sun rises and it's Day time now 🌅\n\n"
        f"Everybody gathers for an emergency meeting. Discuss with others and find the Spy. 🔊\n\n"
        f"Voting will begin in {game.discussion_time} seconds ⏳"
    )
    # CHANGE 6: Send discussion message with image
    try:
        with open('101.jpg', 'rb') as photo:
            await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=discussion_message)
    except Exception as e:
        logger.error(f"Failed to send image 101.jpg: {e}")
        await context.bot.send_message(chat_id=chat_id, text=discussion_message)

    # CHANGE 2: REMOVE the alive players message after discussion
    # This message is completely removed as requested

    await asyncio.sleep(game.discussion_time) # Use custom discussion time

    # Start voting
    await start_voting(context, chat_id)

async def start_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_or_create_game(chat_id)
    
    # Check if game is still active
    if not game.game_active:
        return
    
    round_num = game.current_round

    # Execute Detective kill FIRST and get the result
    detective_target_id, detective_killed_by_spy = await execute_detective_kill(context, chat_id)
    
    # Wait 3 seconds after detective kill
    await asyncio.sleep(3)
    
    # Execute Spy kill SECOND - pass detective kill info
    await execute_spy_kill(context, chat_id, detective_target_id, detective_killed_by_spy)
    
    # Wait 3 seconds, then rest of the function continues exactly the same...
    await asyncio.sleep(3)
    
    # Check again if game is still active after kills
    game = games.get(chat_id)
    if not game or not game.game_active:
        return

    # NOW check win conditions AFTER all kills
    alive_players = [(user_id, name) for user_id, name in game.players if user_id not in game.eliminated_players]
    alive_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
    alive_citizens = [p for p in alive_players if p[0] not in game.spy_user_ids]

    # Check if all spies are eliminated
    if len(alive_spies) == 0:
        # Citizens won
        all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
        
        # Update scores for ALL citizens
        for uid, name in all_citizens:
            if uid not in game.eliminated_players:
                update_player_score(uid, name, won=True, won_as_spy=False, coins=50)
            else:
                update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

        # Update spy scores as losers
        for spy_id in game.spy_user_ids:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            update_player_score(spy_id, spy_name, won=False, won_as_spy=False, coins=0)

        # Winners: alive citizens with roles
        winners_with_roles = []
        for uid, name in all_citizens:
            if uid not in game.eliminated_players:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                winners_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
        
        winners_list = "\n".join(winners_with_roles)

        # Losers: ALL spies (dead and alive) + dead citizens with roles
        losers_with_roles = []
        # Add all spies
        for spy_id in game.spy_user_ids:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            spy_role = game.original_roles.get(spy_id, 'Spy')
            spy_emoji = get_role_emoji(spy_role)
            losers_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
        # Add dead citizens
        for uid, name in all_citizens:
            if uid in game.eliminated_players:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
        
        losers_list = "\n".join(losers_with_roles)

        game_end_message = (
            f"🎉 Citizens won! 🎉\n\n"
            f"Winners :\n\n"
            f"{winners_list}\n\n"
            f"Losers :\n\n"
            f"{losers_list}"
        )
        await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
        
        # Send game end notification
        await notify_game_end(context, chat_id, "Citizens", game.current_round)
        
        # Send words summary after 5 seconds
        asyncio.create_task(send_game_words_summary(context, chat_id, game))
        
        remove_game(chat_id)
        return

    # Check if spies won (citizens <= spies)
    if len(alive_citizens) <= len(alive_spies):
        # Spies won
        all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
        
        # Update scores
        for spy_id in game.spy_user_ids:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            if spy_id not in game.eliminated_players:
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=50)
            else:
                update_player_score(spy_id, spy_name, won=False, won_as_spy=False, coins=0)

        for uid, name in all_citizens:
            update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

        # Winners: alive spies with ORIGINAL roles
        spy_winners_with_roles = []
        for spy_id in game.spy_user_ids:
            if spy_id not in game.eliminated_players:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
        
        spy_winners_list = "\n".join(spy_winners_with_roles)

        # Losers: all citizens with their ORIGINAL roles
        losers_with_roles = []
        for uid, name in all_citizens:
            role = game.original_roles.get(uid, 'Citizen')
            role_emoji = get_role_emoji(role)
            losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
        
        losers_list = "\n".join(losers_with_roles)

        game_end_message = (
            f"🎉 Spy won! 🎉\n\n"
            f"Winners :\n\n"
            f"{spy_winners_list}\n\n"
            f"Losers :\n\n"
            f"{losers_list}"
        )
        await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
        
        # Send game end notification
        await notify_game_end(context, chat_id, "Spies", game.current_round)
        
        # Send words summary after 5 seconds
        asyncio.create_task(send_game_words_summary(context, chat_id, game))
        
        remove_game(chat_id)
        return

    # Disable abilities when discussion starts
    game.spy_kill_available = False
    game.detective_action_available = False
    game.discussion_active = True
    
    # Don't delete ability messages anymore
    logger.info("Abilities disabled, old button clicks will show error message")
    
    discussion_message = (
        f"The sun rises and it's Day time now 🌅\n\n"
        f"Everybody gathers for an emergency meeting. Discuss with others and find the Spy. 🔊\n\n"
        f"Voting will begin in {game.discussion_time} seconds ⏳"
    )
    # Send discussion message with image
    try:
        with open('101.jpg', 'rb') as photo:
            await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=discussion_message)
    except Exception as e:
        logger.error(f"Failed to send image 101.jpg: {e}")
        await context.bot.send_message(chat_id=chat_id, text=discussion_message)

    await asyncio.sleep(game.discussion_time)

    # Check AGAIN if game is still active after discussion time
    game = games.get(chat_id)
    if not game or not game.game_active:
        return

    # Start voting
    game.voting_active = True
    game.votes = {}

    voting_keyboard = []
    # Get updated alive players after potential kills
    alive_players_after_kill = [(user_id, name) for user_id, name in game.players if user_id not in game.eliminated_players]
    for user_id, name in alive_players_after_kill:
        voting_keyboard.append([InlineKeyboardButton(name, callback_data=f"vote_{user_id}", api_kwargs={"style": "primary"})])

    reply_markup = InlineKeyboardMarkup(voting_keyboard)

    voting_message = f"Round {round_num} Voting 🗳️\n\nSelect the person you want to vote for:"

    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text=voting_message,
        reply_markup=reply_markup
    )

    # Set a voting timer
    asyncio.create_task(voting_timer(context, chat_id, round_num))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    help_message = (
        "🕹 ʜᴏᴡ ᴛᴏ ᴘʟᴀʏ? \n\n"
        "ᴛʜɪꜱ ɪꜱ ᴀ ɢᴀᴍᴇ ᴏꜰ ᴛᴇʟʟɪɴɢ ʟɪᴇꜱ 🫠, ᴀɴᴅ ᴡɪɴɴɪɴɢ ʙʏ ᴍᴀɴɪᴘᴜʟᴀᴛɪɴɢ ᴏᴛʜᴇʀꜱ 🔪 \n\n"
        "• ʀᴏʟᴇꜱ :- ᴄɪᴛɪᴢᴇɴ, ꜱᴘʏ\n"
        "• ᴛᴏᴛᴀʟ ʀᴏᴜɴᴅꜱ :- 5\n\n\n"
        "🔵  ʙᴏᴛ ᴡɪʟʟ ɢɪᴠᴇ ʏᴏᴜ ᴏɴᴇ ᴡᴏʀᴅ ɪɴ ᴇᴀᴄʜ ʀᴏᴜɴᴅ, ᴜꜱᴇ /ʜɪɴᴛ ᴛᴏ ɢɪᴠᴇ ʏᴏᴜʀ ʜɪɴᴛ - 2 ᴍɪɴᴜᴛᴇꜱ ᴘᴇʀ ʀᴏᴜɴᴅ (3 ᴍɪɴᴜᴛᴇꜱ ꜰᴏʀ 7+ ᴘʟᴀʏᴇʀꜱ).\n\n"
        "(ᴇᴠᴇʀʏʙᴏᴅʏ ʜᴀꜱ ꜱᴀᴍᴇ ᴡᴏʀᴅꜱ, ꜱᴘʏ ᴡɪʟʟ ʜᴀᴠᴇ ᴅɪꜰꜰᴇʀᴇɴᴛ ᴡᴏʀᴅ ᴅᴏɴ'ᴛ ꜰᴏʀɢᴇᴛ ᴛʜᴀᴛ)\n\n"
        "🔵  ᴀꜰᴛᴇʀ ᴇᴀᴄʜ ʀᴏᴜɴᴅ, ᴛʜᴇʀᴇ ᴡɪʟʟ ʙᴇ ᴠᴏᴛɪɴɢ.\n\n"
        "ʏᴏᴜ ᴄᴀɴ ᴠᴏᴛᴇ ꜰᴏʀ ᴛʜᴇ ꜱᴘʏ \n\n"
        "🔴  ɪꜰ ʏᴏᴜ ʙᴇᴄᴏᴍᴇ ꜱᴘʏ, ʏᴏᴜ ʜᴀᴠᴇ ᴛᴏ ꜰɪɴᴅ ᴛʜᴇ ʀᴇᴀʟ ᴡᴏʀᴅ ꜰʀᴏᴍ ᴏᴛʜᴇʀ ᴘʟᴀʏᴇʀ'ꜱ ᴄʟᴜᴇꜱ.\n\n\n"
        "❗️ꜱᴘʏ ᴄᴀɴ ᴋɪʟʟ ᴏɴᴇ ᴘʟᴀʏᴇʀ ꜰʀᴏᴍ 3ʀᴅ ᴛᴏ 5ᴛʜ ʀᴏᴜɴᴅꜱ! ʙʏ ɢᴏɪɴɢ ɪɴᴛᴏ ʙᴏᴛ'ꜱ ᴅᴍ 💥"
    )
    
    try:
        # Send message in blockquote format
        await update.message.reply_text(f"<blockquote>{help_message}</blockquote>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to send help message with HTML formatting: {e}")
        # Fallback to plain text
        await update.message.reply_text(help_message)



async def send_voting_options_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(60)
        game = get_or_create_game(chat_id)

        keyboard = []
        for user_id, player_name in game.players:
            if user_id not in game.eliminated_players:
                keyboard.append([InlineKeyboardButton(player_name, callback_data=f"vote_{user_id}", api_kwargs={"style": "primary"})])

        if not keyboard:
            await context.bot.send_message(chat_id=chat_id, text="No players to vote for!")
            return

        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text="🗳️ Voting time! Please vote who you think is the Spy by clicking a button below:",
            reply_markup=reply_markup
        )

        asyncio.create_task(voting_timer(context, chat_id))

    except Exception as e:
        logger.error(f"Error in send_voting_options_after_delay: {e}")

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send recent logs to creators only"""
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id

    # Check if user is a creator
    if user_id not in creators:
        await update.message.reply_text("You don't have permission to access logs.")
        return

    try:
        # Read the last 50 lines of logs (adjust as needed)
        log_lines = []

        # If you're using file logging, read from log file
        # For now, we'll create a simple log message
        log_content = f"""📋 **Bot Status Log**
- Active Games: {len(games)}
- Total Users: {len(all_users)}
- Private Users: {len(private_users)}
- Group Chats: {len(all_group_chats)}
- Banned Users: {len(banned_users)}

🎮 **Current Games:**
"""

        for chat_id, game in games.items():
            status = "Lobby" if game.lobby_active else ("Active" if game.game_active else "Inactive")
            log_content += f"Chat {chat_id}: {status} ({len(game.players)} players)\n"

        if not games:
            log_content += "No active games\n"

        await update.message.reply_text(log_content)

    except Exception as e:
        await update.message.reply_text(f"Error getting logs: {str(e)}")

async def voting_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, round_num: int):
    game = games.get(chat_id)
    if not game:
        return
    
    voting_time = game.voting_time
    
    for remaining in range(voting_time, 0, -1):
        await asyncio.sleep(1)
        
        # Check if game still exists and voting is still active
        game = games.get(chat_id)
        if not game or not game.voting_active or not game.game_active:
            return  # Game ended or voting ended, stop timer
        
        # Check if round changed
        if game.current_round != round_num:
            return
        
        # Check if all alive players have voted
        alive_players = [p[0] for p in game.players if p[0] not in game.eliminated_players]
        if len(game.votes) >= len(alive_players):
            # All players voted, end early
            break
    
    # Check one final time before ending voting
    game = games.get(chat_id)
    if not game or not game.voting_active or not game.game_active:
        return
    
    await end_voting(context, chat_id)


# Add this new function
async def notify_game_start(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Send notification when a game starts"""
    try:
        # Get group details
        chat_info = await context.bot.get_chat(chat_id)
        group_name = chat_info.title or "Unknown Group"
        group_link = f"https://t.me/{chat_info.username}" if chat_info.username else "Private group (no public link)"
        
        # Get current time
        current_time = datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
        formatted_date = current_time.strftime("%A, %B %d, %Y")
        
        # Get player count
        game = games.get(chat_id)
        player_count = len(game.players) if game else 0
        
        notification_message = (
            f"🎮 A new game was started:\n\n"
            f"👥 Group name: {group_name}\n"
            f"🔗 Group link: {group_link}\n"
            f"🆔 Group ID: {chat_id}\n"
            f"👤 Players: {player_count}\n\n"
            f"🕐 Time: {formatted_time}\n"
            f"📅 Date: {formatted_date}"
        )
        
        await context.bot.send_message(
            chat_id=-1002771343852,  # FIXED: Use negative ID
            text=notification_message
        )
        logger.info(f"Sent game start notification for chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send game start notification: {e}")


async def send_doctor_save_menu(context: ContextTypes.DEFAULT_TYPE, game: GameState):
    """Send Doctor save menu at start of round"""
    try:
        if not game.doctor_id or game.doctor_id in game.eliminated_players:
            logger.info("Doctor not available for save action")
            return
        
        # Reset save target for new round
        game.doctor_save_target = None
        
        save_message = "Choose whom you will save tonight 💉"
        
        keyboard = []
        alive_players = [(user_id, name) for user_id, name in game.players 
                        if user_id not in game.eliminated_players]
        
        for user_id, name in alive_players:
            # Check if this is the doctor themselves
            if user_id == game.doctor_id:
                # Only show doctor's name if they haven't saved themselves before
                if not game.doctor_saved_self:
                    keyboard.append([InlineKeyboardButton(f"{name} (You)", callback_data=f"doctor_save_{user_id}")])
            else:
                keyboard.append([InlineKeyboardButton(name, callback_data=f"doctor_save_{user_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Always send NEW message each round (don't try to edit old one)
        msg = await context.bot.send_message(
            chat_id=game.doctor_id,
            text=save_message,
            reply_markup=reply_markup
        )
        
        game.doctor_save_message_id = msg.message_id
        logger.info(f"Doctor save menu sent to {game.doctor_id}")
        
    except Exception as e:
        logger.error(f"Failed to send doctor save menu: {e}")

async def rolesinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send roles information as a reply to the user's message"""
    update_user_stats(update)
    
    if not update.effective_chat or not update.message:
        return

    # Check if user is banned
    if update.effective_user and is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    roles_info = """𝗥𝗼𝗹𝗲𝘀:

🧍‍♂️ 𝗖𝗜𝗧𝗜𝗭𝗘𝗡
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗖𝗶𝘁𝗶𝘇𝗲𝗻.
𝗬𝗼𝘂𝗿 𝗴𝗼𝗮𝗹 𝗶𝘀 𝘁𝗼 𝗵𝗲𝗹𝗽 𝘁𝗵𝗲 𝗗𝗲𝘁𝗲𝗰𝘁𝗶𝘃𝗲, 𝗗𝗼𝗰𝘁𝗼𝗿, 𝗞𝗮𝗺𝗶𝗸𝗮𝘇𝗲, 𝗮𝗻𝗱 𝗼𝘁𝗵𝗲𝗿 𝗴𝗼𝗼𝗱 𝗽𝗹𝗮𝘆𝗲𝗿𝘀 𝗸𝗲𝗲𝗽 𝘁𝗵𝗲 𝗰𝗶𝘁𝘆 𝘀𝗮𝗳𝗲 𝗳𝗿𝗼𝗺 𝗰𝗿𝗶𝗺𝗶𝗻𝗮𝗹𝘀.
𝗦𝘁𝗮𝘆 𝗮𝗹𝗲𝗿𝘁 𝗮𝗻𝗱 𝘃𝗼𝘁𝗲 𝘄𝗶𝘀𝗲𝗹𝘆 — 𝘆𝗼𝘂𝗿 𝗰𝗵𝗼𝗶𝗰𝗲𝘀 𝗺𝗮𝘁𝘁𝗲𝗿!


---

🕵️ 𝗗𝗘𝗧𝗘𝗖𝗧𝗜𝗩𝗘
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗗𝗲𝘁𝗲𝗰𝘁𝗶𝘃𝗲.
𝗬𝗼𝘂𝗿 𝗺𝗶𝘀𝘀𝗶𝗼𝗻 𝗶𝘀 𝘁𝗼 𝗶𝗻𝘃𝗲𝘀𝘁𝗶𝗴𝗮𝘁𝗲 𝗮𝗻𝗱 𝗳𝗶𝗻𝗱 𝘁𝗵𝗲 𝗰𝗿𝗶𝗺𝗶𝗻𝗮𝗹𝘀 𝗵𝗶𝗱𝗶𝗻𝗴 𝗮𝗺𝗼𝗻𝗴 𝘁𝗵𝗲 𝗰𝗶𝘁𝗶𝘇𝗲𝗻𝘀.
𝗪𝗼𝗿𝗸 𝘄𝗶𝘁𝗵 𝗼𝘁𝗵𝗲𝗿𝘀 𝘁𝗼 𝘀𝗮𝘃𝗲 𝘁𝗵𝗲 𝗰𝗶𝘁𝘆 𝗮𝗻𝗱 𝗽𝘂𝗻𝗶𝘀𝗵 𝘁𝗵𝗲 𝗴𝘂𝗶𝗹𝘁𝘆.


---

💉 𝗗𝗢𝗖𝗧𝗢𝗥
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗗𝗼𝗰𝘁𝗼𝗿.
𝗘𝗮𝗰𝗵 𝗻𝗶𝗴𝗵𝘁, 𝘆𝗼𝘂 𝗰𝗮𝗻 𝗵𝗲𝗮𝗹 𝗮𝗻𝗱 𝘀𝗮𝘃𝗲 𝗼𝗻𝗲 𝗽𝗹𝗮𝘆𝗲𝗿 𝗳𝗿𝗼𝗺 𝗯𝗲𝗶𝗻𝗴 𝗸𝗶𝗹𝗹𝗲𝗱.
𝗖𝗵𝗼𝗼𝘀𝗲 𝗰𝗮𝗿𝗲𝗳𝘂𝗹𝗹𝘆 — 𝘆𝗼𝘂𝗿 𝗱𝗲𝗰𝗶𝘀𝗶𝗼𝗻 𝗰𝗮𝗻 𝗰𝗵𝗮𝗻𝗴𝗲 𝘁𝗵𝗲 𝗴𝗮𝗺𝗲.


---

💣 𝗞𝗔𝗠𝗜𝗞𝗔𝗭𝗘
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗞𝗮𝗺𝗶𝗸𝗮𝘇𝗲 — 𝘁𝗵𝗲 𝗮𝗻𝗴𝗿𝘆 𝘀𝗲𝗻𝗶𝗼𝗿 𝗰𝗶𝘁𝗶𝘇𝗲𝗻.
𝗜𝗳 𝘀𝗼𝗺𝗲𝗼𝗻𝗲 𝗸𝗶𝗹𝗹𝘀 𝘆𝗼𝘂, 𝘁𝗵𝗲𝘆 𝗱𝗶𝗲 𝘄𝗶𝘁𝗵 𝘆𝗼𝘂!
𝗪𝗵𝗲𝗻 𝘆𝗼𝘂 𝗮𝗿𝗲 𝘃𝗼𝘁𝗲𝗱 𝗼𝘂𝘁, 𝘆𝗼𝘂 𝗰𝗮𝗻 𝗰𝗵𝗼𝗼𝘀𝗲 𝗼𝗻𝗲 𝗽𝗹𝗮𝘆𝗲𝗿 𝘁𝗼 𝘁𝗮𝗸𝗲 𝗱𝗼𝘄𝗻 𝘄𝗶𝘁𝗵 𝘆𝗼𝘂.
𝗨𝘀𝗲 𝘆𝗼𝘂𝗿 𝗿𝗮𝗴𝗲 𝘄𝗶𝘀𝗲𝗹𝘆.


---

🔫 𝗚𝗔𝗡𝗚𝗦𝗧𝗘𝗥
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗚𝗮𝗻𝗴𝘀𝘁𝗲𝗿.
𝗬𝗼𝘂𝗿 𝗷𝗼𝗯 𝗶𝘀 𝘁𝗼 𝗮𝘀𝘀𝗶𝘀𝘁 𝘆𝗼𝘂𝗿 𝗯𝗼𝘀𝘀, 𝘁𝗵𝗲 𝗦𝗽𝘆, 𝗶𝗻 𝗱𝗲𝘀𝘁𝗿𝗼𝘆𝗶𝗻𝗴 𝘁𝗵𝗲 𝗰𝗶𝘁𝘆.
𝗜𝗳 𝘁𝗵𝗲 𝗦𝗽𝘆 𝗱𝗶𝗲𝘀, 𝘆𝗼𝘂 𝗰𝗮𝗻 𝘁𝗮𝗸𝗲 𝗼𝘃𝗲𝗿 𝗮𝘀 𝘁𝗵𝗲 𝗻𝗲𝘄 𝗯𝗼𝘀𝘀.
𝗦𝘁𝗮𝘆 𝗵𝗶𝗱𝗱𝗲𝗻 𝗮𝗻𝗱 𝗹𝗼𝘆𝗮𝗹.


---

🕶️ 𝗦𝗣𝗬
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗧𝗵𝗲 𝗦𝗽𝘆 — 𝘁𝗵𝗲 𝗺𝗮𝗶𝗻 𝘃𝗶𝗹𝗹𝗮𝗶𝗻 𝗼𝗳 𝘁𝗵𝗲 𝗰𝗶𝘁𝘆.
𝗘𝗮𝗰𝗵 𝗻𝗶𝗴𝗵𝘁, 𝘆𝗼𝘂 𝗱𝗲𝗰𝗶𝗱𝗲 𝘄𝗵𝗼 𝘄𝗶𝗹𝗹 𝗻𝗼𝘁 𝘄𝗮𝗸𝗲 𝘂𝗽 𝘁𝗵𝗲 𝗻𝗲𝘅𝘁 𝗺𝗼𝗿𝗻𝗶𝗻𝗴.
𝗟𝗲𝗮𝗱 𝘆𝗼𝘂𝗿 𝗴𝗮𝗻𝗴 𝘀𝘁𝗿𝗮𝘁𝗲𝗴𝗶𝗰𝗮𝗹𝗹𝘆 𝘁𝗼 𝗰𝗼𝗻𝘁𝗿𝗼𝗹 𝗮𝗻𝗱 𝗲𝗹𝗶𝗺𝗶𝗻𝗮𝘁𝗲 𝘁𝗵𝗲 𝗴𝗼𝗼𝗱 𝗽𝗹𝗮𝘆𝗲𝗿𝘀.


---

💻 𝗛𝗔𝗖𝗞𝗘𝗥
𝗬𝗼𝘂 𝗮𝗿𝗲 𝗮 𝗛𝗮𝗰𝗸𝗲𝗿.
𝗘𝗮𝗰𝗵 𝗻𝗶𝗴𝗵𝘁, 𝘆𝗼𝘂 𝗰𝗮𝗻 𝗯𝗹𝗼𝗰𝗸 𝗼𝗻𝗲 𝗽𝗹𝗮𝘆𝗲𝗿'𝘀 𝘀𝗽𝗲𝗰𝗶𝗮𝗹 𝗮𝗯𝗶𝗹𝗶𝘁𝘆 𝗳𝗼𝗿 𝘁𝗵𝗮𝘁 𝗻𝗶𝗴𝗵𝘁.
𝗨𝘀𝗲 𝘆𝗼𝘂𝗿 𝗽𝗼𝘄𝗲𝗿 𝘁𝗼 𝗱𝗶𝘀𝗿𝘂𝗽𝘁 𝗲𝗻𝗲𝗺𝗶𝗲𝘀 𝗼𝗿 𝗽𝗿𝗼𝘁𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗮𝗹𝗹𝗶𝗲𝘀."""

    # Reply to the user's message (quote system)
    await update.message.reply_text(roles_info)


async def send_detective_target_selection(context: ContextTypes.DEFAULT_TYPE, game: GameState, action_type: str):
    """Send target selection menu for Detective"""
    try:
        if not game.detective_id or game.detective_id in game.eliminated_players:
            return
        
        alive_players = [(user_id, name) for user_id, name in game.players 
                        if user_id not in game.eliminated_players and user_id != game.detective_id]
        
        if not alive_players:
            await context.bot.send_message(
                chat_id=game.detective_id,
                text="No players available to target."
            )
            return
        
        selection_message = "Choose one option"
        
        keyboard = []
        for user_id, name in alive_players:
            callback = f"detective_{action_type}_{user_id}"
            keyboard.append([InlineKeyboardButton(name, callback_data=callback)])
        
        # Add Back button
        keyboard.append([InlineKeyboardButton("← Back", callback_data="detective_back")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.edit_message_text(
            chat_id=game.detective_id,
            message_id=game.detective_inspect_message_id,
            text=selection_message,
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Failed to send detective target selection: {e}")

async def send_detective_inspect_result(context: ContextTypes.DEFAULT_TYPE, game: GameState, target_id: int):
    """Send inspect result to Detective after 30 seconds"""
    await asyncio.sleep(30)
    
    try:
        # Check if game still active and detective still alive
        if not game.game_active or game.detective_id not in [p[0] for p in game.players] or game.detective_id in game.eliminated_players:
            return
        
        # Get target name
        target_name = next((name for uid, name in game.players if uid == target_id), "Unknown")
        
        # Check if target is still alive
        if target_id in game.eliminated_players:
            result_message = f"{target_name} is already dead."
        else:
            role = game.player_roles.get(target_id, 'Citizen')
            role_emoji = get_role_emoji(role)
            result_message = f"{target_name} is a {role} {role_emoji}"
        
        await context.bot.send_message(
            chat_id=game.detective_id,
            text=result_message
        )
        
    except Exception as e:
        logger.error(f"Failed to send detective inspect result: {e}")

async def send_detective_action_menu(context: ContextTypes.DEFAULT_TYPE, game: GameState):
    """Send Detective action menu at start of round"""
    try:
        if not game.detective_id or game.detective_id in game.eliminated_players:
            logger.info("Detective not available for action")
            return
        
        # Reset detective targets
        game.detective_inspect_target = None
        game.detective_kill_target = None
        
        game.detective_action_available = True
        
        action_message = "Choose what will you do tonight"
        
        keyboard = [
            [InlineKeyboardButton("Inspect", callback_data="detective_inspect")],
            [InlineKeyboardButton("Kill", callback_data="detective_kill")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Always send NEW message each round (don't try to edit old one)
        msg = await context.bot.send_message(
            chat_id=game.detective_id,
            text=action_message,
            reply_markup=reply_markup
        )
        
        game.detective_inspect_message_id = msg.message_id
        logger.info(f"Detective action menu sent to {game.detective_id}")
        
    except Exception as e:
        logger.error(f"Failed to send detective action menu: {e}")


async def notify_game_end(context: ContextTypes.DEFAULT_TYPE, chat_id: int, winner: str, total_rounds: int):
    """Send notification when a game ends"""
    try:
        # Get group details
        chat_info = await context.bot.get_chat(chat_id)
        group_name = chat_info.title or "Unknown Group"
        group_link = f"https://t.me/{chat_info.username}" if chat_info.username else "Private group (no public link)"
        
        # Get current time
        current_time = datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
        formatted_date = current_time.strftime("%A, %B %d, %Y")
        
        # Get player count
        game = games.get(chat_id)
        player_count = len(game.players) if game else 0
        
        notification_message = (
            f"🏁 Game ended:\n\n"
            f"👥 Group name: {group_name}\n"
            f"🔗 Group link: {group_link}\n"
            f"🆔 Group ID: {chat_id}\n"
            f"👤 Players: {player_count}\n"
            f"🏆 Winner: {winner}\n"
            f"🔄 Total rounds: {total_rounds}\n\n"
            f"🕐 Time: {formatted_time}\n"
            f"📅 Date: {formatted_date}"
        )
        
        await context.bot.send_message(
            chat_id=GAME_NOTIFICATION_CHAT_ID,
            text=notification_message
        )
        logger.info(f"Sent game end notification for chat {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send game end notification: {e}")

async def handle_last_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle last words from players killed by spy"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Check if user is banned
    if is_user_banned(user_id):
        return
    
    # Find the game where this user was killed by spy
    user_game = None
    user_chat_id = None
    
    for chat_id, game in games.items():
        if game.game_active and user_id in game.last_words_eligible:
            user_game = game
            user_chat_id = chat_id
            break
    
    if not user_game:
        # User is not eligible for last words in any game
        return
    
    # Check if 60 seconds have passed since they were killed
    import time
    kill_timestamp = user_game.last_words_eligible[user_id]
    current_time = time.time()
    time_elapsed = current_time - kill_timestamp
    
    if time_elapsed > 60:
        # Too late
        await update.message.reply_text("You already died before you could speak a word")
        # Remove from eligible list
        del user_game.last_words_eligible[user_id]
        return
    
    # Get player name
    player_name = next((name for uid, name in user_game.players if uid == user_id), "Unknown")
    
    # Send last words to the game chat
    last_words_message = f"<b>{player_name} shouted before dying,\n\"{message_text}\"</b>"
    
    try:
        await context.bot.send_message(
            chat_id=user_chat_id,
            text=last_words_message,
            parse_mode='HTML'
        )
        logger.info(f"Sent last words from {player_name} ({user_id}) to chat {user_chat_id}")
        
        # Confirm to user
        await update.message.reply_text("Your last words have been delivered.")
        
        # Remove from eligible list after they've spoken
        del user_game.last_words_eligible[user_id]
        
    except Exception as e:
        logger.error(f"Failed to send last words: {e}")
        await update.message.reply_text("Failed to deliver your last words.")


async def end_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_or_create_game(chat_id)
    
    if not game.voting_active:
        return  # Already ended
        
    game.voting_active = False
    coins_reward = 50

    # Get alive players for validation
    alive_players = [(user_id, name) for user_id, name in game.players if user_id not in game.eliminated_players]
    alive_player_ids = [user_id for user_id, name in alive_players]
    alive_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]

    # Handle no votes or 5th round special cases
    if not game.votes or game.current_round >= 5:
        if game.current_round >= 5:
            # 5th round - if no votes or tie, spies win
            if not game.votes:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Voting is over ⏳\n\n\nCitizens couldn't find the Spy in the final round."
                )
            
            # Spies won at round 5
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=coins_reward)

            # Include ALL players in winning message with roles
            all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
            
            for uid, name in all_citizens:
                update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

            # Winners: alive spies with ORIGINAL roles
            spy_winners_with_roles = []
            for spy_id in alive_spies:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
            
            spy_winners_list = "\n".join(spy_winners_with_roles)
            
            # Losers: all citizens with their ORIGINAL roles
            losers_with_roles = []
            for uid, name in all_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            losers_list = "\n".join(losers_with_roles)

            game_end_message = (
                f"🎉 Spy won! 🎉\n\n"
                f"Winners :\n\n"
                f"{spy_winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )
            
            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Spies", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return
        else:
            # Before round 5 - continue game
            await context.bot.send_message(
                chat_id=chat_id,
                text="Voting is over ⏳\n\n\nCitizens couldn't find the Spy today."
            )

            # Send next round message
            next_round = game.current_round + 1
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Round {next_round} will be started after few seconds 🔴\n\nSleep tight when the deadly night arrives 🌉"
            )
            await asyncio.sleep(15)

            await continue_to_next_round(context, chat_id)
            return

    # Count valid votes only (from alive players)
    vote_counts = {}
    valid_votes = {}
    
    for voter_id, voted_for_id in game.votes.items():
        # Only count votes from alive players for alive players
        if voter_id in alive_player_ids and voted_for_id in alive_player_ids:
            valid_votes[voter_id] = voted_for_id
            vote_counts[voted_for_id] = vote_counts.get(voted_for_id, 0) + 1

    if not vote_counts:
        # No valid votes - handle same as no votes above
        if game.current_round >= 5:
            # Spies win in round 5
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=coins_reward)

            # Include ALL players in winning message with roles
            all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
            
            for uid, name in all_citizens:
                update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

            # Winners: alive spies with ORIGINAL roles
            spy_winners_with_roles = []
            for spy_id in alive_spies:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
            
            spy_winners_list = "\n".join(spy_winners_with_roles)
            
            # Losers: all citizens with their ORIGINAL roles
            losers_with_roles = []
            for uid, name in all_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            losers_list = "\n".join(losers_with_roles)

            await context.bot.send_message(
                chat_id=chat_id,
                text="Voting is over ⏳\n\n\nNo valid votes! Citizens couldn't decide in the final round."
            )

            game_end_message = (
                f"🎉 Spy won! 🎉\n\n"
                f"Winners :\n\n"
                f"{spy_winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )
            
            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Spies", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return
        else:
            # Before round 5 - continue game
            await context.bot.send_message(
                chat_id=chat_id,
                text="Voting is over ⏳\n\n\nNo valid votes! Citizens couldn't decide."
            )

            # Send next round message
            next_round = game.current_round + 1
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Round {next_round} will be started after few seconds 🔴\n\nSleep tight when the deadly night arrives 🌉"
            )
            await asyncio.sleep(15)

            await continue_to_next_round(context, chat_id)
            return

    # Find who got most votes
    max_votes = max(vote_counts.values())
    most_voted = [user_id for user_id, count in vote_counts.items() if count == max_votes]

    # Check for tie
    if len(most_voted) > 1:
        if game.current_round >= 5:
            # Round 5 tie - spies win
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=coins_reward)

            # Include ALL players in winning message with roles
            all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
            
            for uid, name in all_citizens:
                update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

            # Winners: alive spies with ORIGINAL roles
            spy_winners_with_roles = []
            for spy_id in alive_spies:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
            
            spy_winners_list = "\n".join(spy_winners_with_roles)
            
            # Losers: all citizens with their ORIGINAL roles
            losers_with_roles = []
            for uid, name in all_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            losers_list = "\n".join(losers_with_roles)

            tie_names = [next(name for uid, name in alive_players if uid == user_id) for user_id in most_voted]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Voting is over ⏳\n\n\nTie between {', '.join(tie_names)}! Citizens couldn't decide in the final round."
            )

            game_end_message = (
                f"🎉 Spy won! 🎉\n\n"
                f"Winners :\n\n"
                f"{spy_winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )
            
            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Spies", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return
        else:
            # Before round 5 - continue game
            tie_names = [next(name for uid, name in alive_players if uid == user_id) for user_id in most_voted]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Voting is over ⏳\n\n\nTie between {', '.join(tie_names)}! Citizens couldn't decide."
            )

            # Send next round message
            next_round = game.current_round + 1
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Round {next_round} will be started after few seconds 🔴\n\nSleep tight when the deadly night arrives 🌉"
            )
            await asyncio.sleep(15)

            await continue_to_next_round(context, chat_id)
            return

    # Someone got the most votes
    eliminated_player_id = most_voted[0]
    eliminated_player_name = next(name for uid, name in alive_players if uid == eliminated_player_id)

    game.eliminated_players.add(eliminated_player_id)
    
    # Check if voted player is Kamikaze - send revenge menu
    if eliminated_player_id == game.kamikaze_id:
        import time
        game.kamikaze_revenge_timestamp = time.time()
        
        # Send kamikaze revenge menu
        try:
            alive_for_revenge = [(uid, name) for uid, name in game.players 
                               if uid not in game.eliminated_players and uid != game.kamikaze_id]
            
            if alive_for_revenge:
                revenge_message = "Choose whom you want to die with you 💣\n\nYou have 60 seconds!"
                
                keyboard = []
                for uid, name in alive_for_revenge:
                    keyboard.append([InlineKeyboardButton(name, callback_data=f"kamikaze_revenge_{uid}")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                msg = await context.bot.send_message(
                    chat_id=game.kamikaze_id,
                    text=revenge_message,
                    reply_markup=reply_markup
                )
                
                game.kamikaze_revenge_message_id = msg.message_id
                logger.info(f"Kamikaze revenge menu sent to {game.kamikaze_id}")
                
                # Start timeout task
                asyncio.create_task(kamikaze_revenge_timeout(context, game, chat_id))
        except Exception as e:
            logger.error(f"Failed to send kamikaze revenge menu: {e}")

    # FIXED: Get eliminated player's role and show in bold HTML
    eliminated_role = game.original_roles.get(eliminated_player_id, 'Citizen')
    eliminated_role_emoji = get_role_emoji(eliminated_role)
    
    # Check if eliminated player was a spy
    if eliminated_player_id in game.spy_user_ids:
        # Spy was eliminated, check if all spies are gone
        remaining_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
        
        if not remaining_spies:
            # All spies eliminated - citizens win
            remaining_citizens = [(uid, name) for uid, name in alive_players if uid not in game.spy_user_ids and uid not in game.eliminated_players]
            
            for uid, name in remaining_citizens:
                update_player_score(uid, name, won=True, won_as_spy=False, coins=coins_reward)

            # Eliminated spy doesn't get reward
            update_player_score(eliminated_player_id, eliminated_player_name, won=False, won_as_spy=False, coins=0)

            # Other eliminated spies also don't get reward
            for spy_id in game.spy_user_ids:
                if spy_id != eliminated_player_id:
                    spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                    update_player_score(spy_id, spy_name, won=False, won_as_spy=False, coins=0)

            # Winners: alive citizens with ORIGINAL roles
            winners_with_roles = []
            for uid, name in remaining_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                winners_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            winners_list = "\n".join(winners_with_roles)
            
            # Losers: all spies (dead and alive) + dead citizens with ORIGINAL roles
            losers_with_roles = []
            # Add all spies
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                losers_with_roles.append((spy_id, spy_name, spy_role, spy_emoji))
            # Add dead citizens
            for uid, name in game.players:
                if uid in game.eliminated_players and uid not in game.spy_user_ids:
                    role = game.original_roles.get(uid, 'Citizen')
                    role_emoji = get_role_emoji(role)
                    losers_with_roles.append((uid, name, role, role_emoji))
            
            losers_list = "\n".join([f"[{name}](tg://user?id={uid}) - {role} {emoji}" for uid, name, role, emoji in losers_with_roles])

            # FIXED: Show role in bold HTML
            voting_result_msg = f"Voting is over ⏳\n\n\n{eliminated_player_name} was <b>{eliminated_role} {eliminated_role_emoji}</b>"
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=voting_result_msg,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send voting result with HTML: {e}")
                # Fallback without HTML
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Voting is over ⏳\n\n\n{eliminated_player_name} was {eliminated_role} {eliminated_role_emoji}"
                )

            game_end_message = (
                f"🎉 Citizens won! 🎉\n\n"
                f"Winners :\n\n"
                f"{winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )

            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Citizens", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return
        else:
            # Some spies still remain
            # FIXED: Show role in bold HTML
            voting_result_msg = f"Voting is over ⏳\n\n\n{eliminated_player_name} was <b>{eliminated_role} {eliminated_role_emoji}</b>"
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=voting_result_msg,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send voting result with HTML: {e}")
                # Fallback without HTML
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Voting is over ⏳\n\n\n{eliminated_player_name} was {eliminated_role} {eliminated_role_emoji}"
                )
    else:
        # Citizen was eliminated
        # FIXED: Show role in bold HTML
        voting_result_msg = f"Voting is over ⏳\n\n\n{eliminated_player_name} was <b>{eliminated_role} {eliminated_role_emoji}</b>"
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=voting_result_msg,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Failed to send voting result with HTML: {e}")
            # Fallback without HTML
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Voting is over ⏳\n\n\n{eliminated_player_name} was {eliminated_role} {eliminated_role_emoji}"
            )

    # Check win conditions after elimination
    remaining_alive = [(uid, name) for uid, name in alive_players if uid not in game.eliminated_players]
    remaining_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
    remaining_citizens = [p for p in remaining_alive if p[0] not in game.spy_user_ids]

    if len(remaining_spies) >= len(remaining_citizens):
        # Spies win (equal or outnumber citizens)
        for spy_id in game.spy_user_ids:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            if spy_id not in game.eliminated_players:
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=coins_reward)
            else:
                update_player_score(spy_id, spy_name, won=False, won_as_spy=False, coins=0)

        all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
        for uid, name in all_citizens:
            update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

        # Winners: alive spies with ORIGINAL roles
        spy_winners_with_roles = []
        for spy_id in remaining_spies:
            spy_name = next(name for user_id, name in game.players if user_id == spy_id)
            spy_role = game.original_roles.get(spy_id, 'Spy')
            spy_emoji = get_role_emoji(spy_role)
            spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
        
        spy_winners_list = "\n".join(spy_winners_with_roles)
        
        # Losers: all citizens with their ORIGINAL roles
        losers_with_roles = []
        for uid, name in all_citizens:
            role = game.original_roles.get(uid, 'Citizen')
            role_emoji = get_role_emoji(role)
            losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
        
        losers_list = "\n".join(losers_with_roles)

        game_end_message = (
            f"🎉 Spy won! 🎉\n\n"
            f"Winners :\n\n"
            f"{spy_winners_list}\n\n"
            f"Losers :\n\n"
            f"{losers_list}"
        )

        await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
        
        # Send game end notification
        await notify_game_end(context, chat_id, "Spies", game.current_round)
        
        # Send words summary after 5 seconds
        asyncio.create_task(send_game_words_summary(context, chat_id, game))
        
        remove_game(chat_id)
        return

    # Continue to next round
    next_round = game.current_round + 1
    if next_round <= 5:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Round {next_round} will be started after few seconds 🔴\n\nSleep tight when the deadly night arrives 🌉"
        )
        await asyncio.sleep(15)
        await continue_to_next_round(context, chat_id)
    else:
        # Game ended after 5 rounds
        remove_game(chat_id)

async def kamikaze_revenge_timeout(context: ContextTypes.DEFAULT_TYPE, game: GameState, chat_id: int):
    """Handle Kamikaze revenge timeout after 60 seconds"""
    await asyncio.sleep(60)
    
    # Check if Kamikaze still hasn't selected a target
    if game.kamikaze_revenge_target is None and game.kamikaze_id:
        try:
            await context.bot.send_message(
                chat_id=game.kamikaze_id,
                text="You died before you could take someone with you! ⏰"
            )
            logger.info(f"Kamikaze {game.kamikaze_id} timed out without selecting revenge target")
        except Exception as e:
            logger.error(f"Failed to send kamikaze timeout message: {e}")
        
        # Clear the timestamp
        game.kamikaze_revenge_timestamp = None

async def continue_to_next_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_or_create_game(chat_id)
    
    if not game.game_active:
        return

    game.current_round += 1
    game.current_player_index = 0
    game.votes = {}

    # Check if game should end after 5 rounds
    if game.current_round > 5:
        # Game ends, check who won
        alive_spies = [spy_id for spy_id in game.spy_user_ids if spy_id not in game.eliminated_players]
        
        if alive_spies:
            # Spies survived 5 rounds - they win
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                update_player_score(spy_id, spy_name, won=True, won_as_spy=True, coins=50)

            # Include ALL players in winning message with roles
            all_citizens = [(uid, name) for uid, name in game.players if uid not in game.spy_user_ids]
            
            for uid, name in all_citizens:
                update_player_score(uid, name, won=False, won_as_spy=False, coins=0)

            # Winners: alive spies with ORIGINAL roles
            spy_winners_with_roles = []
            for spy_id in alive_spies:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                spy_winners_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
            
            spy_winners_list = "\n".join(spy_winners_with_roles)
            
            # Losers: all citizens with their ORIGINAL roles
            losers_with_roles = []
            for uid, name in all_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            losers_list = "\n".join(losers_with_roles)

            game_end_message = (
                f"🎉 Spy won! 🎉\n\n"
                f"Winners :\n\n"
                f"{spy_winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )
            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Spies", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return
        else:
            # Citizens won
            alive_citizens = [(uid, name) for uid, name in game.players if uid not in game.eliminated_players and uid not in game.spy_user_ids]
            
            for uid, name in alive_citizens:
                update_player_score(uid, name, won=True, won_as_spy=False, coins=50)

            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                update_player_score(spy_id, spy_name, won=False, won_as_spy=False, coins=0)

            # Winners: alive citizens with ORIGINAL roles
            winners_with_roles = []
            for uid, name in alive_citizens:
                role = game.original_roles.get(uid, 'Citizen')
                role_emoji = get_role_emoji(role)
                winners_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            winners_list = "\n".join(winners_with_roles)
            
            # Losers: all spies (dead and alive) + dead citizens with ORIGINAL roles
            losers_with_roles = []
            # Add all spies
            for spy_id in game.spy_user_ids:
                spy_name = next(name for user_id, name in game.players if user_id == spy_id)
                spy_role = game.original_roles.get(spy_id, 'Spy')
                spy_emoji = get_role_emoji(spy_role)
                losers_with_roles.append(f"[{spy_name}](tg://user?id={spy_id}) - {spy_role} {spy_emoji}")
            # Add dead citizens
            for uid, name in game.players:
                if uid in game.eliminated_players and uid not in game.spy_user_ids:
                    role = game.original_roles.get(uid, 'Citizen')
                    role_emoji = get_role_emoji(role)
                    losers_with_roles.append(f"[{name}](tg://user?id={uid}) - {role} {role_emoji}")
            
            losers_list = "\n".join(losers_with_roles)

            game_end_message = (
                f"🎉 Citizens won! 🎉\n\n"
                f"Winners :\n\n"
                f"{winners_list}\n\n"
                f"Losers :\n\n"
                f"{losers_list}"
            )
            await context.bot.send_message(chat_id=chat_id, text=game_end_message, parse_mode='Markdown')
            
            # Send game end notification
            await notify_game_end(context, chat_id, "Citizens", game.current_round)
            
            # Send words summary after 5 seconds
            asyncio.create_task(send_game_words_summary(context, chat_id, game))
            
            remove_game(chat_id)
            return

    # Assign new words for the new round using assign_words_for_round
    assign_words_for_round(game)
    logger.info(f"New words assigned for round {game.current_round}: Citizen={game.citizen_word}, Spy={game.spy_word}")

    # Send GAME STARTED status message for the new round (rounds 2, 3, 4, 5)
    if game.current_round >= 2:
        status_text = build_game_status_message(game)

        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        bot_dm_link = f"https://t.me/{bot_username}"

        game_keyboard = [
            [
                InlineKeyboardButton("Check the word", callback_data="check_word", api_kwargs={"style": "success"}),
                InlineKeyboardButton("Go to bot", url=bot_dm_link, api_kwargs={"style": "primary"})
            ]
        ]
        game_reply_markup = InlineKeyboardMarkup(game_keyboard)

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=status_text,
                reply_markup=game_reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to send round status message: {e}")

    # Start the new round
    await start_round(context, chat_id)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = get_or_create_game(chat_id)

    can_stop = False

    if user_id == game.admin_user_id:
        can_stop = True
    else:
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status in ['administrator', 'creator']:
                can_stop = True
        except:
            pass

    if not can_stop:
        return

    if game.lobby_active or game.game_active:
        await update.message.reply_text("Game stopped by admin.")
        remove_game(chat_id)

async def notify_group_join(context: ContextTypes.DEFAULT_TYPE, chat_id: int, chat_title: str, added_by_user):
    """Send notification to logs group when bot is added to a new group"""
    try:
        # Get group link if possible
        try:
            chat_info = await context.bot.get_chat(chat_id)
            group_link = f"https://t.me/{chat_info.username}" if chat_info.username else "Private group (no public link)"
        except:
            group_link = "Unable to retrieve group link"
        
        # Get current time
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Create notification message
        notification_message = (
            f"Bot has joined a group!\n"
            f"Group name: {chat_title}\n"
            f"Group link: {group_link}\n\n"
            f"Time: {current_time}\n"
            f"Person who added: [{added_by_user.first_name}](tg://user?id={added_by_user.id})"
        )
        
        await context.bot.send_message(
            chat_id=LOGS_CHAT_ID,
            text=notification_message,
            parse_mode='Markdown'
        )
        logger.info(f"Sent group join notification for chat {chat_id}")
        
    except Exception as e:
        logger.error(f"Failed to send group join notification: {e}")

async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle when bot is added to a group"""
    if not update.message or not update.message.new_chat_members:
        return
    
    # Check if the bot itself was added
    bot_info = await context.bot.get_me()
    for member in update.message.new_chat_members:
        if member.id == bot_info.id:
            # Bot was added to this group
            chat_title = update.effective_chat.title or "Unknown Group"
            added_by = update.effective_user
            
            if added_by:
                await notify_group_join(context, update.effective_chat.id, chat_title, added_by)
            break

async def send_role_introduction_messages(context: ContextTypes.DEFAULT_TYPE, game: GameState):
    """Send role introduction messages to all players immediately after /begin command"""
    
    role_messages = {
        'Citizen': "You're <b>Citizen 👥</b>\nYou will help Detective, Doctor, Kamikaze etc people to keep the city safe 🏙️",
        'Detective': "You're <b>Detective 🕵</b>\nYour mission is to save the city from criminals, and punish them",
        'Doctor': "You're <b>Doctor 👨‍⚕</b>\nYou can heal and save one player each night",
        'Kamikaze': "You're <b>Kamikaze 💣</b>\nThe angry senior citizen\nYou can kill one person when you're voted out\nIf someone kills you, they'll die with you! 🧨",
        'Gangster': "You're <b>Gangster 🥷</b>\nYour task is to work for your boss, Spy\nIf spy dies, you can become new boss",
        'Spy': "You're <b>The Spy 🕶</b>\nMain villain of the city 🌆\nYou will decide who wakes up next morning!",
        'Hacker': "You're <b>Hacker 👨‍💻</b>\nYou can stop someone from using abilities, for one night 🌉"
    }
    
    # Send to all players
    for user_id, name in game.players:
        # Get player's role
        player_role = game.original_roles.get(user_id, 'Citizen')
        
        # Get the message for this role
        message = role_messages.get(player_role, "You're <b>Citizen 👥</b>")
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='HTML'
            )
            logger.info(f"Role introduction sent to {name} ({player_role})")
        except Exception as e:
            logger.error(f"Failed to send role introduction to {user_id}: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if not query.from_user or not query.message:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    # Check if user is banned
    if is_user_banned(user_id):
        await query.answer("You have been banned from using this bot.", show_alert=True)
        return

    # Handle join game button FIRST
    if data == "join_game":
        await query.answer("Joining game...")
        class FakeUpdate:
            def __init__(self, user, chat):
                self.effective_user = user
                self.effective_chat = chat
        fake_update = FakeUpdate(query.from_user, query.message.chat)
        await handle_join_game(fake_update, context)
        return

    # Handle Detective actions EARLY (before settings which also check for admin)
    if data == "detective_inspect":
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.detective_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Detective!", show_alert=True)
            return
        
        if not user_game.detective_action_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return
        
        await send_detective_target_selection(context, user_game, "inspect")
        await query.answer()
        return
    
    if data == "detective_kill":
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.detective_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Detective!", show_alert=True)
            return
        
        if not user_game.detective_action_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return
        
        await send_detective_target_selection(context, user_game, "kill")
        await query.answer()
        return
    
    if data.startswith("detective_inspect_"):
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.detective_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Detective!", show_alert=True)
            return
        
        if not user_game.detective_action_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return
        
        target_id = int(data.split("_")[-1])
        
        if target_id in user_game.eliminated_players:
            await query.answer("That player is already eliminated!", show_alert=True)
            return
        
        if target_id == user_id:
            await query.answer("You cannot inspect yourself!", show_alert=True)
            return
        
        user_game.detective_inspect_target = target_id
        target_name = next((name for uid, name in user_game.players if uid == target_id), "Unknown")
        
        await query.edit_message_text(f"You will inspect {target_name} in 30 seconds")
        await query.answer()
        
        asyncio.create_task(send_detective_inspect_result(context, user_game, target_id))
        return
    
    if data.startswith("detective_kill_"):
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.detective_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Detective!", show_alert=True)
            return
        
        if not user_game.detective_action_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return
        
        target_id = int(data.split("_")[-1])
        
        if target_id in user_game.eliminated_players:
            await query.answer("That player is already eliminated!", show_alert=True)
            return
        
        if target_id == user_id:
            await query.answer("You cannot kill yourself!", show_alert=True)
            return
        
        user_game.detective_kill_target = target_id
        target_name = next((name for uid, name in user_game.players if uid == target_id), "Unknown")
        
        await query.edit_message_text(f"You've selected {target_name} to kill")
        await query.answer()
        return

    # Handle Detective back button
    if data == "detective_back":
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.detective_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Detective!", show_alert=True)
            return
        
        # Send back the main detective menu
        action_message = "Choose what will you do tonight"
        keyboard = [
            [InlineKeyboardButton("Inspect", callback_data="detective_inspect")],
            [InlineKeyboardButton("Kill", callback_data="detective_kill")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(action_message, reply_markup=reply_markup)
        await query.answer()
        return

    # Handle Doctor save action
    if data.startswith("doctor_save_"):
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.doctor_id and user_id not in game.eliminated_players:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Doctor!", show_alert=True)
            return
        
        # Check if night phase is still active
        if not user_game.spy_kill_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return
        
        target_id = int(data.split("_")[-1])
        
        if target_id in user_game.eliminated_players:
            await query.answer("That player is already eliminated!", show_alert=True)
            return
        
        # Check if doctor is trying to save themselves
        if target_id == user_id and user_game.doctor_saved_self:
            await query.answer("You have already saved yourself once! You cannot save yourself again.", show_alert=True)
            return
        
        # Set the save target
        user_game.doctor_save_target = target_id
        
        # If doctor saved themselves, mark it
        if target_id == user_id:
            user_game.doctor_saved_self = True
        
        target_name = next((name for uid, name in user_game.players if uid == target_id), "Unknown")
        
        await query.edit_message_text(f"You will save {target_name} tonight 💉")
        await query.answer()
        return

    # Handle Kamikaze revenge selection
    if data.startswith("kamikaze_revenge_"):
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and user_id == game.kamikaze_id:
                user_game = game
                break
        
        if not user_game:
            await query.answer("You are not the Kamikaze!", show_alert=True)
            return
        
        # Check if 1 minute has passed
        import time
        if user_game.kamikaze_revenge_timestamp and (time.time() - user_game.kamikaze_revenge_timestamp) > 60:
            await query.answer("You died before you could take someone with you!", show_alert=True)
            await query.edit_message_text("You died before you could take someone with you!")
            return
        
        target_id = int(data.split("_")[-1])
        
        if target_id in user_game.eliminated_players:
            await query.answer("That player is already eliminated!", show_alert=True)
            return
        
        user_game.kamikaze_revenge_target = target_id
        target_name = next((name for uid, name in user_game.players if uid == target_id), "Unknown")
        
        await query.edit_message_text(f"💣 You will take {target_name} with you to death!")
        await query.answer()
        
        # Execute revenge after 30 seconds
        asyncio.create_task(delayed_kamikaze_revenge(context, chat_id, user_game))
        return

    # Handle Spy kill actions EARLY (before settings)
    if data.startswith("kill_"):
        user_game = None
        for game_chat_id, game in games.items():
            if game.game_active and (user_id in game.spy_user_ids or user_id in game.gangster_ids) and user_id not in game.eliminated_players:
                user_game = game
                break

        if not user_game:
            await query.answer("You are not in the spy team!", show_alert=True)
            return

        if not user_game.spy_kill_available:
            await query.answer("You're clicking on old buttons, come back later", show_alert=True)
            return

        kill_target_id = int(data.split("_")[1])
        
        if kill_target_id in user_game.eliminated_players:
            await query.answer("That player is already eliminated!", show_alert=True)
            return

        if kill_target_id == user_id:
            await query.answer("You cannot kill yourself!", show_alert=True)
            return

        if kill_target_id in user_game.spy_user_ids or kill_target_id in user_game.gangster_ids:
            await query.answer("You cannot kill your teammate!", show_alert=True)
            return

        user_game.spy_kill_targets[user_id] = kill_target_id
        target_name = next(name for uid, name in user_game.players if uid == kill_target_id)
        
        # Send confirmation to the player who made the selection
        await query.answer(f"You have selected {target_name} to kill!", show_alert=True)
        await query.edit_message_text(f"You've selected {target_name} for kill 🔪")
        
        # Get selector's role and name
        selector_name = next((name for uid, name in user_game.players if uid == user_id), "Unknown")
        selector_role = user_game.original_roles.get(user_id, 'Spy')
        selector_role_emoji = get_role_emoji(selector_role)
        
        # Notify all spy team members about this selection (FIXED: Don't send to selector)
        spy_team = []
        
        # Add spy if alive
        for spy_id in user_game.spy_user_ids:
            if spy_id not in user_game.eliminated_players and spy_id != user_id:  # Don't send to selector
                spy_team.append(spy_id)
        
        # Add gangsters if alive
        for gangster_id in user_game.gangster_ids:
            if gangster_id not in user_game.eliminated_players and gangster_id != user_id:  # Don't send to selector
                spy_team.append(gangster_id)
        
        # Send notification to all team members
        team_notification = f"{selector_role} {selector_role_emoji} - {selector_name} has selected {target_name} to kill"
        
        for member_id in spy_team:
            try:
                await context.bot.send_message(
                    chat_id=member_id,
                    text=team_notification
                )
            except Exception as e:
                logger.error(f"Failed to send team notification to {member_id}: {e}")
        
        return

    # NOW handle settings (these need admin checks in GROUP chats)
    if data == "main_time_settings":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        game = get_or_create_game(chat_id)
        
        settings_text = (
            f"⚙️ Current Game Settings:\n\n"
            f"🕐 Turn Time: {game.turn_time} seconds\n"
            f"💬 Discussion Time: {game.discussion_time} seconds\n"
            f"🗳️ Voting Time: {game.voting_time} seconds\n\n"
            f"Click below to change settings:"
        )

        keyboard = [
            [InlineKeyboardButton("Change turn time", callback_data="settings_turn_time")],
            [InlineKeyboardButton("Change discussion time", callback_data="settings_discussion_time")],
            [InlineKeyboardButton("Change voting time", callback_data="settings_voting_time")],
            [InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(settings_text, reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "main_message_deletion":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        settings_text = "Choose any option:"
        
        keyboard = [
            [InlineKeyboardButton("Dead people Message deletion", callback_data="dead_people_messages")],
            [InlineKeyboardButton("Word deletion", callback_data="word_deletion")],
            [InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(settings_text, reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "main_reset_stats":
        await query.answer("This feature is coming soon!", show_alert=True)
        return

    elif data == "dead_people_messages":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        settings_text = "Do you want dead people to write in the chat after they're dead?"
        
        keyboard = [
            [InlineKeyboardButton("Yes", callback_data="dead_messages_yes")],
            [InlineKeyboardButton("No", callback_data="dead_messages_no")],
            [InlineKeyboardButton("Back", callback_data="main_message_deletion")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(settings_text, reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "word_deletion":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        settings_text = "Do you want messages which contains words of citizens and Spy, get deleted?"
        
        keyboard = [
            [InlineKeyboardButton("Yes", callback_data="word_delete_yes")],
            [InlineKeyboardButton("No", callback_data="word_delete_no")],
            [InlineKeyboardButton("Back", callback_data="main_message_deletion")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(settings_text, reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "dead_messages_yes":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        game = get_or_create_game(chat_id)
        game.dead_people_can_write = True

        keyboard = [[InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("✅ Dead people can write in chat!", reply_markup=reply_markup)
        await query.answer("Setting updated!")
        return

    elif data == "dead_messages_no":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        game = get_or_create_game(chat_id)
        game.dead_people_can_write = False

        keyboard = [[InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("✅ Dead people will be silenced and cannot write in chat!", reply_markup=reply_markup)
        await query.answer("Setting updated!")
        return

    elif data == "word_delete_yes":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        game = get_or_create_game(chat_id)
        game.delete_word_messages = True

        keyboard = [[InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("✅ Messages containing game words will be deleted!", reply_markup=reply_markup)
        await query.answer("Setting updated!")
        return

    elif data == "word_delete_no":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        game = get_or_create_game(chat_id)
        game.delete_word_messages = False

        keyboard = [[InlineKeyboardButton("Back to Main Settings", callback_data="back_to_main_settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("✅ Messages containing game words will NOT be deleted!", reply_markup=reply_markup)
        await query.answer("Setting updated!")
        return

    elif data == "back_to_main_settings":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        settings_text = "⚙️ Choose what you want to change:"
        
        keyboard = [
            [InlineKeyboardButton("Time settings", callback_data="main_time_settings")],
            [InlineKeyboardButton("Message deletion", callback_data="main_message_deletion")],
            [InlineKeyboardButton("Reset your stats (upcoming feature)", callback_data="main_reset_stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(settings_text, reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "settings_turn_time":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        keyboard = [
            [InlineKeyboardButton("15 seconds", callback_data="set_turn_time_15")],
            [InlineKeyboardButton("30 seconds", callback_data="set_turn_time_30")],
            [InlineKeyboardButton("45 seconds", callback_data="set_turn_time_45")],
            [InlineKeyboardButton("60 seconds", callback_data="set_turn_time_60")],
            [InlineKeyboardButton("90 seconds", callback_data="set_turn_time_90")],
            [InlineKeyboardButton("Back to Time Settings", callback_data="main_time_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text("Select Turn Time:", reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "settings_discussion_time":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        keyboard = [
            [InlineKeyboardButton("15 seconds", callback_data="set_discussion_time_15")],
            [InlineKeyboardButton("30 seconds", callback_data="set_discussion_time_30")],
            [InlineKeyboardButton("45 seconds", callback_data="set_discussion_time_45")],
            [InlineKeyboardButton("60 seconds", callback_data="set_discussion_time_60")],
            [InlineKeyboardButton("90 seconds", callback_data="set_discussion_time_90")],
            [InlineKeyboardButton("Back to Time Settings", callback_data="main_time_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text("Select Discussion Time:", reply_markup=reply_markup)
        await query.answer()
        return

    elif data == "settings_voting_time":
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        keyboard = [
            [InlineKeyboardButton("15 seconds", callback_data="set_voting_time_15")],
            [InlineKeyboardButton("30 seconds", callback_data="set_voting_time_30")],
            [InlineKeyboardButton("45 seconds", callback_data="set_voting_time_45")],
            [InlineKeyboardButton("60 seconds", callback_data="set_voting_time_60")],
            [InlineKeyboardButton("90 seconds", callback_data="set_voting_time_90")],
            [InlineKeyboardButton("Back to Time Settings", callback_data="main_time_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text("Select Voting Time:", reply_markup=reply_markup)
        await query.answer()
        return

    elif data.startswith("set_"):
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['administrator', 'creator']:
                await query.answer("Only group admins can change settings!", show_alert=True)
                return
        except:
            await query.answer("Only group admins can change settings!", show_alert=True)
            return

        parts = data.split("_")
        if len(parts) < 4:
            await query.answer("Invalid setting!", show_alert=True)
            return
            
        setting_type = parts[1] + "_" + parts[2]
        time_value = int(parts[3])
        
        game = get_or_create_game(chat_id)
        
        if setting_type == "turn_time":
            game.turn_time = time_value
            setting_name = "Turn time"
        elif setting_type == "discussion_time":
            game.discussion_time = time_value
            setting_name = "Discussion time"
        elif setting_type == "voting_time":
            game.voting_time = time_value
            setting_name = "Voting time"
        else:
            await query.answer("Invalid setting!", show_alert=True)
            return

        time_display = f"{time_value} seconds"
        keyboard = [[InlineKeyboardButton("Back to Time Settings", callback_data="main_time_settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ {setting_name} has been set to {time_display}!",
            reply_markup=reply_markup
        )
        await query.answer(f"{setting_name} updated!")
        return

    # Game actions at the end
    game = get_or_create_game(chat_id)

    if data == "check_word":
        if not game.game_active or user_id not in [p[0] for p in game.players]:
            await query.answer("You are not in an active game!", show_alert=True)
            return

        if user_id in game.eliminated_players:
            await query.answer(text="You have been eliminated and cannot see your word!", show_alert=True)
            return

        role = game.player_roles.get(user_id, 'Citizen')
        role_emoji = get_role_emoji(role)
        
        # Spy and Gangsters get spy_word
        if user_id in game.spy_user_ids or user_id in game.gangster_ids:
            word_message = f"Your role: {role} {role_emoji}\nYour word: {game.spy_word}"
        else:
            word_message = f"Your role: {role} {role_emoji}\nYour word: {game.citizen_word}"
        
        await query.answer(text=word_message, show_alert=True)

    elif data.startswith("vote_"):
        if not game.voting_active:
            await query.answer("Voting is not active right now!", show_alert=True)
            return

        voted_for_user_id = int(data.split("_")[1])

        if user_id not in [p[0] for p in game.players]:
            await query.answer("You are not in this game!", show_alert=True)
            return

        if user_id in game.eliminated_players:
            await query.answer("You are eliminated and cannot vote!", show_alert=True)
            return

        if user_id in game.votes:
            await query.answer(text="You have already voted! You can only vote once.", show_alert=True)
            return

        game.votes[user_id] = voted_for_user_id

        voter_name = next(name for uid, name in game.players if uid == user_id)
        voted_for_name = next(name for uid, name in game.players if uid == voted_for_user_id)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"[{voter_name}](tg://user?id={user_id}) voted for [{voted_for_name}](tg://user?id={voted_for_user_id})",
            parse_mode='Markdown'
        )

        await query.answer(f"You voted for {voted_for_name}!")

async def ping_command(update, context):
    if not update.effective_user:
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    if not chat:
        return

    if is_user_banned(user_id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    # Access the game state for this chat
    game = games.get(chat.id)

    # Respond only if no active game or lobby in this chat
    if game and (game.lobby_active or game.game_active):
        return # silent no response while game is active

    ping_message = (
        "System is online 🤖\n\n"
        "City is burning under the threat ⚠️\n\n"
        "The dead ones are coming 🧟‍♂️🧟‍♀️🧟"
    )
    
    try:
        with open('pin.jpg', 'rb') as photo:
            await context.bot.send_photo(chat_id=chat.id, photo=photo, caption=ping_message)
    except Exception as e:
        logger.error(f"Failed to send image pin.jpg: {e}")
        await context.bot.send_message(chat_id=chat.id, text=ping_message)

async def hints_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle hints page navigation"""
    query = update.callback_query
    
    if not query.from_user or not query.message:
        return
    
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    # Check if user is banned
    if is_user_banned(user_id):
        await query.answer("You have been banned from using this bot.", show_alert=True)
        return
    
    game = get_or_create_game(chat_id)
    
    if not game.game_active:
        await query.answer("No active game!", show_alert=True)
        return
    
    # Extract page number from callback data
    data = query.data
    if not data.startswith("hints_page_"):
        return
    
    try:
        page = int(data.split("_")[-1])
    except:
        await query.answer("Invalid page!", show_alert=True)
        return
    
    # Get alive players
    alive_players = [(idx, player_id, player_name) for idx, (player_id, player_name) in enumerate(game.players, 1) if player_id not in game.eliminated_players]
    
    # Build hints for this page
    players_per_page = 4
    total_pages = (len(alive_players) + players_per_page - 1) // players_per_page
    
    # Validate page number
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    
    # Get players for this page
    start_idx = page * players_per_page
    end_idx = min(start_idx + players_per_page, len(alive_players))
    page_players = alive_players[start_idx:end_idx]
    
    # Build hints message
    hints_message = f"🔍 Hints (Page {page + 1}/{total_pages}):\n\n"
    
    for idx, player_id, player_name in page_players:
        hints_message += f"{idx}) {player_name}\n\n"
        
        player_hints = game.player_hints.get(player_id, [])
        if player_hints:
            for hint in player_hints:
                safe_hint = hint.replace('`', '\\`').replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')
                hints_message += f"<blockquote>{safe_hint}</blockquote>\n"
            hints_message += "\n"
        else:
            hints_message += "(No hints yet)\n\n"
    
    # Create navigation buttons
    keyboard = []
    nav_buttons = []
    
    # Add "GO BACK" button if not on first page
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ GO BACK", callback_data=f"hints_page_{page - 1}"))
    
    # Add "NEXT PAGE" button if not on last page
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("NEXT PAGE ▶️", callback_data=f"hints_page_{page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    try:
        # Edit the message with new page
        await query.edit_message_text(hints_message, parse_mode='HTML', reply_markup=reply_markup)
        await query.answer(f"Page {page + 1}/{total_pages}")
    except Exception as e:
        logger.error(f"Failed to edit hints page: {e}")
        # Fallback to simple text
        try:
            simple_hints = f"🔍 Hints (Page {page + 1}/{total_pages}):\n\n"
            for idx, player_id, player_name in page_players:
                simple_hints += f"{idx}) {player_name}\n\n"
                player_hints = game.player_hints.get(player_id, [])
                if player_hints:
                    simple_hints += "\n".join(player_hints) + "\n\n"
                else:
                    simple_hints += "(No hints yet)\n\n"
            
            await query.edit_message_text(simple_hints, reply_markup=reply_markup)
            await query.answer(f"Page {page + 1}/{total_pages}")
        except Exception as e2:
            logger.error(f"Failed to edit hints even without formatting: {e2}")
            await query.answer("Error updating page!", show_alert=True)


async def handle_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send notification when bot is added to a new group"""
    if not update.my_chat_member:
        return
    
    new_status = update.my_chat_member.new_chat_member.status
    old_status = update.my_chat_member.old_chat_member.status
    chat = update.effective_chat
    
    # Check if bot was just added to the group
    if old_status in ['left', 'kicked'] and new_status in ['member', 'administrator']:
        try:
            # Get group details
            group_name = chat.title or "Unknown Group"
            group_id = chat.id
            group_type = chat.type  # 'group' or 'supergroup'
            
            # Get who added the bot
            added_by = update.my_chat_member.from_user
            added_by_name = added_by.full_name
            added_by_username = f"@{added_by.username}" if added_by.username else "No username"
            added_by_id = added_by.id
            
            # Try to get invite link
            try:
                invite_link = await context.bot.export_chat_invite_link(chat.id)
            except:
                invite_link = "Unable to get invite link"
            
            # Get member count
            try:
                member_count = await context.bot.get_chat_member_count(chat.id)
            except:
                member_count = "Unknown"
            
            # Get group description
            try:
                chat_full = await context.bot.get_chat(chat.id)
                description = chat_full.description or "No description"
            except:
                description = "No description"
            
            # Get current time
            from datetime import datetime
            current_time = datetime.utcnow()
            formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            formatted_date = current_time.strftime("%A, %B %d, %Y")
            
            # Bot status
            bot_status = "Administrator" if new_status == 'administrator' else "Member"
            
            # Prepare detailed notification message
            notification_message = (
                f"🤖 Bot Added to New Group!\n\n"
                f"👥 Group Details:\n"
                f"📌 Name: {group_name}\n"
                f"🆔 ID: {group_id}\n"
                f"📊 Type: {group_type.title()}\n"
                f"👤 Members: {member_count}\n"
                f"📝 Description: {description[:100]}{'...' if len(description) > 100 else ''}\n"
                f"🔗 Invite Link: {invite_link}\n\n"
                f"👤 Added By:\n"
                f"📛 Name: {added_by_name}\n"
                f"🔖 Username: {added_by_username}\n"
                f"🆔 User ID: {added_by_id}\n\n"
                f"⚙️ Bot Status: {bot_status}\n\n"
                f"🕐 Time: {formatted_time}\n"
                f"📅 Date: {formatted_date}"
            )
            
            # Send to your notification group
            NOTIFICATION_GROUP_ID = -1002771343852  # Updated group ID
            
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATION_GROUP_ID,
                    text=notification_message
                )
                logger.info(f"Bot added to group: {group_name} ({group_id}) by {added_by_name} - Notification sent")
            except Exception as notify_error:
                logger.error(f"Failed to send group join notification: {notify_error}")
                logger.info(f"Bot added to group: {group_name} ({group_id}) - Could not send notification")
            
        except Exception as e:
            logger.error(f"Failed to send new group notification: {e}")


# NEW COMMAND: Enhanced ban user command
async def banuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    if user_id not in creators:
        await update.message.reply_text("Only bot creators can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /banuser <user_id> or reply to a user's message with /banuser")
        return

    target_user_id = None

    # Try to get user ID from arguments
    if context.args[0].isdigit():
        target_user_id = int(context.args[0])
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
    else:
        await update.message.reply_text("Please provide a valid user ID or reply to a user's message.")
        return

    if target_user_id in creators:
        await update.message.reply_text("Cannot ban a bot creator!")
        return

    banned_users.add(target_user_id)
    save_banned_users()

    # Try to get user info
    try:
        user_info = await context.bot.get_chat(target_user_id)
        username = user_info.first_name or user_info.username or str(target_user_id)
        await update.message.reply_text(f"User {username} (ID: {target_user_id}) has been banned from the bot.")
    except:
        await update.message.reply_text(f"User ID {target_user_id} has been banned from the bot.")

    logger.info(f"User {target_user_id} was banned by creator {user_id}")

# NEW COMMAND: Logs command for creators
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    if user_id not in creators:
        await update.message.reply_text("Only bot creators can use this command.")
        return

    try:
        # Get current statistics
        total_users = len(all_users)
        total_groups = len(all_group_chats)
        total_banned = len(banned_users)
        active_games = len([g for g in games.values() if g.game_active])
        active_lobbies = len([g for g in games.values() if g.lobby_active])

        # Get recent log entries (you might want to implement a proper log file reader)
        log_message = f"""📊 **Bot Statistics & Logs**

**User Statistics:**
👥 Total Users: {total_users}
🏠 Total Groups: {total_groups}
🚫 Banned Users: {total_banned}

**Game Statistics:**
🎮 Active Games: {active_games}
🏁 Active Lobbies: {active_lobbies}

**System Status:**
✅ Bot Status: Running
🕐 Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**Recent Activity:**
- Games running in {len(games)} chats
- Player scores tracked for {len(player_scores)} users
"""

        # Add information about current games
        if games:
            log_message += "\n**Active Games Details:**\n"
            for chat_id, game in games.items():
                status = "Game" if game.game_active else "Lobby" if game.lobby_active else "Inactive"
                players_count = len(game.players)
                round_num = game.current_round if game.game_active else 0
                log_message += f"• Chat {chat_id}: {status} - {players_count} players - Round {round_num}\n"

        await update.message.reply_text(log_message, parse_mode='Markdown')

        # Send banned users list if any
        if banned_users:
            banned_list = "🚫 **Banned Users:**\n"
            for banned_id in list(banned_users)[:10]: # Limit to first 10
                banned_list += f"• {banned_id}\n"
            if len(banned_users) > 10:
                banned_list += f"... and {len(banned_users) - 10} more"
            await update.message.reply_text(banned_list, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in logs command: {e}")
        await update.message.reply_text(f"Error retrieving logs: {str(e)}")

async def joinok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-join command for testing - creator only"""
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check if user is creator
    if user_id not in creators:
        await update.message.reply_text("Only bot creators can use this command.")
        return

    # Check if in group
    if update.effective_chat.type == 'private':
        await update.message.reply_text("This command can only be used in groups!")
        return

    game = get_or_create_game(chat_id)

    # Check if lobby is active
    if not game.lobby_active or game.game_active:
        await update.message.reply_text("No active lobby to join!")
        return

    # Test account IDs and names
    test_accounts = [
        (8359169768, "Test Player 1"),
        (8048280735, "Test Player 2"),
        (7076655709, "Test Player 3"),
        (7928118177, "Test Player 4"),
        (7053815721, "Test Player 5"),
        (675001209, "Test Player 6"),
        (5701144658, "Test Player 7")
    ]

    for test_id, test_name in test_accounts:
        # Check if already joined
        if test_id in [p[0] for p in game.players]:
            continue

        # Check if game is full
        if len(game.players) >= 20:
            break

        # Add player
        game.players.append((test_id, test_name))

    # Update lobby message
    players_list = "\n".join([f"{i+1}) [{name}](tg://user?id={user_id})" for i, (user_id, name) in enumerate(game.players)])

    updated_message = f"""Game started 🔪

Players : 

{players_list}

Use /begin to start the game"""

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    deep_link_url = f"https://t.me/{bot_username}?start=join_{chat_id}"
    
    keyboard = [[InlineKeyboardButton("JOIN GAME 🎮", url=deep_link_url, api_kwargs={"style": "success"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game.host_message_id,
            text=updated_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except:
        pass

    await update.message.reply_text("✅ Test accounts joined!")


# NEW SETTINGS COMMANDS
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user or not update.message:
        return

    # Check if user is banned
    if is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    if update.effective_chat.type == 'private':
        await update.message.reply_text("Settings can only be changed in groups!")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if user is admin
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ['administrator', 'creator']:
            await update.message.reply_text("Only group admins can change settings!")
            return
    except:
        await update.message.reply_text("Only group admins can change settings!")
        return

    # Main settings menu
    settings_text = "⚙️ Choose what you want to change:"
    
    keyboard = [
        [InlineKeyboardButton("Time settings", callback_data="main_time_settings")],
        [InlineKeyboardButton("Message deletion", callback_data="main_message_deletion")],
        [InlineKeyboardButton("Reset your stats (upcoming feature)", callback_data="main_reset_stats")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(settings_text, reply_markup=reply_markup)


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
        
    if update.effective_user.id not in creators:
        await update.message.reply_text("Only creators can use this command.")
        return

    total_groups = len(all_group_chats)
    await update.message.reply_text(f"The bot is currently in {total_groups} groups.")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    # If there is an active game in this chat, show hints to everyone
    if update.effective_chat and update.effective_chat.type in ['group', 'supergroup']:
        chat_id = update.effective_chat.id
        game = games.get(chat_id)
        if game and game.game_active:
            # Build hints list message
            hints_lines = ""
            any_hints = False
            for player_id, player_name in game.players:
                if player_id in game.eliminated_players:
                    continue
                player_hints_list = game.player_hints.get(player_id, [])
                if player_hints_list:
                    any_hints = True
                    last_hint = player_hints_list[-1]
                    hints_lines += f"🔹 {player_name}:  \n\"{last_hint}\"\n\n"

            if any_hints:
                hints_section = hints_lines
            else:
                hints_section = "(No hints yet)\n\n"

            list_message = (
                "💬 𝗛𝗶𝗻𝘁𝘀:\n\n"
                "━━━━━━━━━━━━━━━\n\n"
                f"{hints_section}"
                "━━━━━━━━━━━━━━━\n\n"
                "⌨️ Type /hint (your hint) to add your hint in this list"
            )
            await update.message.reply_text(list_message)
            return

    # Admin-only: show groups list
    if update.effective_user.id not in creators:
        await update.message.reply_text("Only creators can use this command.")
        return

    args = context.args
    page = 1
    if args and args[0].isdigit():
        page = int(args[0])

    groups_list = list(all_group_chats)
    if not groups_list:
        await update.message.reply_text("No groups found.")
        return

    per_page = 10
    total_pages = (len(groups_list) + per_page - 1) // per_page

    if page > total_pages or page < 1:
        await update.message.reply_text("Invalid page number.")
        return

    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, len(groups_list))
    page_groups = groups_list[start_idx:end_idx]

    text_lines = [f"{start_idx + i + 1}. Group ID: {gid}" for i, gid in enumerate(page_groups)]
    text = "Groups where the bot is present:\n\n" + "\n".join(text_lines)

    buttons = []
    if page < total_pages:
        buttons = [[InlineKeyboardButton("Next", callback_data=f"list_page_{page + 1}")]]
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    await update.message.reply_text(text, reply_markup=reply_markup)

async def list_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not user or user.id not in creators:
        await query.answer("You're not authorized.", show_alert=True)
        return

    data = query.data
    if not data.startswith("list_page_"):
        return

    page = int(data.split("_")[-1])
    groups_list = list(all_group_chats)

    per_page = 10
    total_pages = (len(groups_list) + per_page -1) // per_page
    if page > total_pages or page < 1:
        await query.answer("Invalid page.")
        return

    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, len(groups_list))
    page_groups = groups_list[start_idx:end_idx]

    text_lines = [f"{start_idx + i + 1}. Group ID: {gid}" for i, gid in enumerate(page_groups)]
    text = "Groups where the bot is present:\n\n" + "\n".join(text_lines)

    buttons = []
    if page < total_pages:
        buttons = [[InlineKeyboardButton("Next", callback_data=f"list_page_{page + 1}")]]
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        pass
    await query.answer()

# Creator/Admin commands

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in creators:
        await update.message.reply_text("You are not authorized to use /broadcast.")
        return
    if context.args:
        msg = ' '.join(context.args)
        failed = []
        success_count = 0
        for chat_id in all_group_chats:
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg)
                success_count += 1
            except Exception as e:
                failed.append(chat_id)
                logger.error(f"Failed to send broadcast to {chat_id}: {e}")
        
        result_msg = f"Broadcast sent to {success_count} groups."
        if failed:
            result_msg += f" Failed: {len(failed)} group(s)."
        await update.message.reply_text(result_msg)
    else:
        await update.message.reply_text("Usage: /broadcast <message>")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    user_id = update.effective_user.id if update.effective_user else None
    
    # Only original creator (675001209) can use this command
    if user_id != 675001209:
        await update.message.reply_text("Only the bot creator can promote others.")
        return

    promoted_ids = set()
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        promoted_ids.add(update.message.reply_to_message.from_user.id)
    
    if not promoted_ids:
        await update.message.reply_text("Tag a user's message in reply to promote them as creator.")
        return
    
    for pid in promoted_ids:
        if pid != 675001209:  # Don't add the original creator again
            creators.add(pid)
    
    await update.message.reply_text(f"Promoted user {', '.join(str(pid) for pid in promoted_ids)} as bot creator with limited privileges (cannot promote others).")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user:
        return
    if update.effective_user.id not in creators:
        await update.message.reply_text("Only creators can use this command.")
        return
    await update.message.reply_text(f"Total unique users interacted: {len(all_users)}")

async def pm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    if not update.effective_user:
        return
    if update.effective_user.id not in creators:
        await update.message.reply_text("Only creators can use this command.")
        return
    await update.message.reply_text(f"Total users interacted in private DM: {len(private_users)}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    user = update.effective_user
    if not user:
        return

    # Check if user is banned
    if is_user_banned(user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    user_id = str(user.id)
    data = player_scores.get(user_id)
    if not data:
        await update.message.reply_text(f"No stats found for you yet.")
        return
    
    name = data.get("name", "Unknown")
    cash = data.get("cash", 0)
    games_played = data.get("games_played", 0)
    games_won = data.get("games_won", 0)
    spy_wins = data.get("spy_wins", 0)
    
    # Calculate global rank
    sorted_players = sorted(player_scores.items(), key=lambda x: x[1].get("cash", 0), reverse=True)
    global_rank = 1
    for rank, (uid, _) in enumerate(sorted_players, start=1):
        if uid == user_id:
            global_rank = rank
            break
    
    msg = (
        f"📊 User Stats\n\n"
        f"👤 Name: {name}\n"
        f"💰 Cash: {cash}\n"
        f"🎮 Total Games: {games_played}\n"
        f"🏆 Winnings: {games_won}\n"
        f"🥷 Winnings as Spy: {spy_wins}\n"
        f"🌍 Global Rank: #{global_rank}"
    )
    
    try:
        # Send with blockquote formatting
        await update.message.reply_text(f"<blockquote>{msg}</blockquote>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to send stats with HTML formatting: {e}")
        # Fallback to plain text
        await update.message.reply_text(msg)

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    
    # Check if user is banned
    if update.effective_user and is_user_banned(update.effective_user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    if not player_scores:
        await update.message.reply_text("No leaderboard data available.")
        return

    top_players = sorted(player_scores.items(), key=lambda x: x[1].get("cash", 0), reverse=True)

    msg = "Leaderboard 🏆\n\nTop 3 :\n\n"
    for i, (uid, data) in enumerate(top_players[:3], start=1):
        msg += f"{i}) {data.get('name','Unknown')} - {data.get('cash',0)}💰\n"

    if len(top_players) > 3:
        msg += "\nOthers :\n\n"
        for i, (uid, data) in enumerate(top_players[3:10], start=4):
            msg += f"{i}) {data.get('name','Unknown')} - {data.get('cash',0)}💰\n"

    try:
        # Send entire message in blockquote
        await update.message.reply_text(f"<blockquote>{msg}</blockquote>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to send leaderboard with HTML formatting: {e}")
        # Fallback to plain text
        await update.message.reply_text(msg)

async def supdate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_stats(update)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in creators:
        await update.message.reply_text("Only bot creators can reload the scoreboard.")
        return

    global player_scores
    if os.path.exists(SCORES_FILE):
        try:
            with open(SCORES_FILE, 'r') as f:
                loaded_scores = json.load(f)
                if isinstance(loaded_scores, dict):
                    player_scores = loaded_scores
                    await update.message.reply_text("Scoreboard has been reloaded from file.")
                else:
                    await update.message.reply_text("scores.json is not a dict, cannot reload.")
        except Exception as e:
            logger.error(f"Failed to reload scores: {e}")
            await update.message.reply_text("Failed to load scores.json file.")
    else:
        await update.message.reply_text("scores.json file not found.")

# CHANGE 2: Add reload command for creators
async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
        
    user_id = update.effective_user.id

    if user_id not in creators:
        await update.message.reply_text("Only bot creators can use this command.")
        return

    try:
        # Save current scores to pscores.json
        with open('pscores.json', 'w') as f:
            json.dump(player_scores, f, indent=4)
        
        await update.message.reply_text("✅ Scores have been saved to pscores.json file successfully!")
        logger.info(f"Creator {user_id} used /reload command to save scores")
        
    except Exception as e:
        logger.error(f"Failed to save scores to pscores.json: {e}")
        await update.message.reply_text(f"❌ Failed to save scores: {str(e)}")

def main():
    # Load games from file on startup
    load_games_from_file()
    
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return
    
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("contact", contact_command))
    
    application.add_handler(CommandHandler("host", host_command))
    application.add_handler(CommandHandler("begin", begin_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("hint", hint_command))
    application.add_handler(CommandHandler("alive", alive_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("banuser", banuser_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("reload", reload_command))
    application.add_handler(CommandHandler("joinok", joinok_command))
    application.add_handler(CommandHandler("rolesinfo", rolesinfo_command))
    
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("pm", pm_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("supdate", supdate_command))
    
    # IMPORTANT: Add specific callback handlers BEFORE the general button_callback
    # This ensures pattern-matching handlers are checked first
    application.add_handler(CallbackQueryHandler(hints_pagination_callback, pattern="^hints_page_"))
    application.add_handler(CallbackQueryHandler(list_pagination_callback, pattern="^list_page_"))
    
    # General callback handler - MUST be last among callback handlers
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Add message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, turn_text_message_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    
    # Add handler for when bot is added to groups
    application.add_handler(ChatMemberHandler(handle_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # Start periodic save task and resume games after bot starts
    async def post_init(application):
        asyncio.create_task(periodic_save_games(None))
        
        # Store context in bot_data for use in resume function
        from telegram.ext import ContextTypes
        application.bot_data['context'] = application
            
    application.post_init = post_init
    
    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    main()