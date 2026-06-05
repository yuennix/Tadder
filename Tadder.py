from sys import argv
from time import sleep
from os import system as term, environ
try:
    from platform import system
except:
    system("pip install platform")
#____________________________________________________________________________________

apiId = int(environ.get("TELEGRAM_API_ID", 0))
apiHash = environ.get("TELEGRAM_API_HASH", "")

#____________________________________________________________________________________
blu = "\033[96m"
red = "\033[91m"
grn = "\033[32m"
ylw = "\033[93m"
res = "\033[0;m"

#____________________________________________________________________________________
help = f"""
    {red}Usage {ylw}: {grn}python Tadder.py [OPTION] ...
    {ylw}Adds members of another group to another group

    Mandatory arguments to long options are mandatory for short options too
        {red}-h {ylw}, {red}--help          {grn}display this help and exit
        {red}-e {ylw}, {red}--ex            {grn}member extraction
        {red}-a {ylw}, {red}--add           {grn}Add members (username or invite link)

    {ylw}Use help 

        {red}[{blu}Extract{red}] {grn}python {red}Tadder.py {ylw}--ex{red}/{ylw}-e 
        {red}[{blu}Add{red}] {grn}python {red}Tadder.py {ylw}--add{red}/{ylw}-a {red}<{grn}Group username or invite link{red}>

    {ylw}Examples:
        {grn}python Tadder.py --add mygroup
        {grn}python Tadder.py --add https://t.me/+JO-8L4UT7gIyNzc1

"""

asciiArt = f"""{red}
 ____   _       ____    __  __  _      _____   ___   __ __ 
|    \ | T     /    T  /  ]|  l/ ]    |     | /   \ |  T  T
|  o  )| |    Y  o  | /  / |  ' /     |   __jY     Y|  |  |
|     T| l___ |     |/  /  |    \     |  l_  |  O  |l_   _j
|  O  ||     T|  _  /   \_ |     Y    |   _] |     ||     |
|     ||     ||  |  \     ||  .  |    |  T   l     !|  |  |
l_____jl_____jl__j__j\____jl__j\_j    l__j    \___/ |__j__|
            {grn}Telegram {blu}:  {red}@BlackFoxSecurityTeam  
            {blu} Coded By MrB4rCod & Maximum Radikali
{res}"""

#____________________________________________________________________________________
def clear():
    sleep(0.2)
    if system() == "Windows":
        term('cls')
    elif system() == "Linux" or system() == "Darwin":
        term('clear')

#____________________________________________________________________________________
def check_credentials():
    if not apiId or not apiHash:
        print(f"{red}[!] {ylw}Missing Telegram API credentials!")
        print(f"{grn}    Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your environment secrets.")
        print(f"{blu}    Get them from: {grn}https://my.telegram.org/auth{res}")
        exit(1)

#____________________________________________________________________________________
def parse_invite_link(gpId):
    """
    Returns (invite_hash, None) for private invite links,
    or (None, username) for plain usernames/public links.
    """
    link = gpId.strip()
    for prefix in ["https://t.me/+", "http://t.me/+", "t.me/+",
                   "https://t.me/joinchat/", "http://t.me/joinchat/", "t.me/joinchat/"]:
        if link.startswith(prefix):
            return link[len(prefix):], None
    if link.startswith("https://t.me/") or link.startswith("t.me/"):
        username = link.split("/")[-1]
        return None, username
    return None, link

#____________________________________________________________________________________
from telethon.sync import TelegramClient, events
from telethon.tl.functions.messages import AddChatUserRequest, ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import InputPeerUser
import telethon
#____________________________________________________________________________________
client = TelegramClient("BlackFox", apiId, apiHash)
gp_lists = []
i = 0

#_________________________________{ Scraper }________________________________________
def Scraper():
    check_credentials()
    client.start()
    clear()
    print(asciiArt)
    global i
    dialogs = client.get_dialogs()
    for dialog in dialogs:
        if dialog.is_group == True:
            try:
                if dialog.entity.username:
                    gp_lists.append(str(dialog.entity.username))
            except:
                continue

    for pla in gp_lists:
        i += 1
        print(f"{red}{str(i)} {blu}➜ {grn}{pla}")

    usgp = input(f"\n{red}❯❯{blu} ")
    users = client.get_participants(gp_lists[int(usgp)], limit=5000)
    open("members.txt", "w").write("")
    user_me = client.get_me().username
    for user in users:
        if user.username != None:
            if "bot" in user.username:
                continue
            else:
                if user_me == str(user.username):
                    pass
                else:
                    open("members.txt", "a").write(str(user.username) + "\n")

    print(f"\n{blu}[{ylw}!{blu}] {red}Member has been fetched now ")

#__________________________________{ Adder }__________________________________________
def adder(gpId):
    check_credentials()
    client.start()
    clear()
    print(asciiArt)

    invite_hash, username = parse_invite_link(gpId)

    if invite_hash:
        print(f"{blu}[{ylw}*{blu}] {grn}Detected private invite link, resolving group...")
        try:
            invite_info = client(CheckChatInviteRequest(invite_hash))
            try:
                input_channel = invite_info.chat
            except AttributeError:
                result = client(ImportChatInviteRequest(invite_hash))
                input_channel = result.chats[0]
        except telethon.errors.rpcerrorlist.UserAlreadyParticipantError:
            result = client(ImportChatInviteRequest(invite_hash))
            input_channel = result.chats[0]
        except Exception as e:
            try:
                input_channel = client.get_entity(invite_hash)
            except Exception:
                print(f"{red}[!] {ylw}Could not resolve invite link: {e}{res}")
                return
    else:
        input_channel = client.get_entity(username)

    peer_channel = PeerChannel(input_channel.id)
    user = open("members.txt", "r")
    i = 0
    for userr in user:
        i += 1
        try:
            peer_user = client.get_input_entity(userr.strip())
            client(InviteToChannelRequest(peer_channel, users=[peer_user]))
            username_clean = userr.replace('\n', " ")
            print(f"{blu}[{red}{str(i)}{blu}] {grn}{username_clean}")
        except telethon.errors.rpcerrorlist.UserPrivacyRestrictedError:
            print(f"{blu}[{red}-{blu}] {red}Error from user privacy {ylw}!")
            continue
        except telethon.errors.rpcerrorlist.PeerFloodError:
            print(f"{blu}[{red}-{blu}] {red}Error from Peer Flood {ylw}!")
            sleep(5)
            continue
        except telethon.errors.rpcerrorlist.FloodWaitError:
            print(f"{blu}[{ylw}!{blu}] {red}Wait for long time 20 min {ylw}!")
            sleep(60 * 20)
            continue
        except telethon.errors.rpcerrorlist.UserChannelsTooMuchError:
            continue
        except telethon.errors.rpcerrorlist.UserNotMutualContactError:
            continue
#____________________________________________________________________________________
if(len(argv) <= 1):
    clear()
    print(asciiArt)
    print(help)
elif(len(argv) >= 1):
    if argv[1] == "--help" or argv[1] == "-h":
        clear()
        print(asciiArt)
        print(help)
    elif argv[1] == "--ex" or argv[1] == "-e":
        if (len(argv) <= 2):
            clear()
            Scraper()
        else:
            clear()
            print(asciiArt)
            print(help)
    elif argv[1] == "--add" or argv[1] == "-a":
        if (len(argv) <= 3):
            clear()
            adder(argv[2])
        else:
            clear()
            print(asciiArt)
            print(help)
    else:
        clear()
        print(asciiArt)
        print(help)
