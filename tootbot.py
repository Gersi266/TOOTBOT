import os.path
import sys
import re
import sqlite3
from datetime import datetime, timedelta
from time import sleep
import feedparser
from mastodon import Mastodon
import requests

# Program configuration
auth_file = "tootbot.auth"
auth_session = auth_file+'.session'
mastodon_api = None

# Set to 1 to get some messages, 0 for error messages only
debug=1

# Posting delay (s), wait between mastodon posts, reduces the "burst" effect on timoeline, and instance workload if you hit rate limiters
posting_delay=1

# URL substrings dedicated to source image hosting
twitter_pics_hosting="https://pbs.twitimg.com/"
nitter_pics_hosting="https://nitter.net/pic/"

# Program logic below this line

if len(sys.argv) < 4:
    print("Usage: python3 tootbot.py [full path to config file] twitter_feed_or_twitter_account max_days footer_tags delay")  # noqa
    sys.exit(1)

auth_file = sys.argv[1]
auth_session = auth_file+'.secret'
source = sys.argv[2]
days = int(sys.argv[3])
tags = sys.argv[4]
delay = int(sys.argv[5])

# Returns the parameter from the specified file
def get_config(parameter, file_path):
    # Check if secrets file exists
    if not os.path.isfile(file_path):
        print("ERROR: Config file (%s) not found"%file_path)
        sys.exit(0)

    # Find parameter in file
    with open( file_path ) as f:
        for line in f:
            if line.startswith( parameter ):
                return line.replace(parameter + ":", "").strip()

    # Cannot find parameter, exit
    print(file_path + "  Missing parameter %s "%parameter)
    sys.exit(0)
# end get_config()

# Look for credentials in the coniguration file
auth_type = get_config("auth_type",auth_file)
if "token" in auth_type:
    # We are using an application token (developer options in the mastodon account, new app...)
    app_client_id = get_config("app_client_id",auth_file)
    app_client_secret = get_config("app_client_secret",auth_file)
    app_access_token  = get_config("app_access_token",auth_file)
else:
    if "email" in auth_type:
        # We are using theuser account credential
        mastodon_email_account = get_config("mastodon_email_account",auth_file)
        mastodon_email_password = get_config("mastodon_email_password",auth_file)
    else:
        print("ERROR: Check the configuration file, no authentication method found")
        sys.exit(1)

instance = get_config("instance",auth_file)
mastodon_account = get_config("mastodon_account",auth_file)+'@'+instance

# sqlite db to store processed tweets (and corresponding toots ids)
sql = sqlite3.connect(os.path.dirname(auth_file) + '/tootbot' + mastodon_account + '.db')
db = sql.cursor()
db.execute('''CREATE TABLE IF NOT EXISTS tweets (tweet text, toot text, twitter text, mastodon text, instance text)''')

if source[:4] == 'http':
    if debug: print("Parsing source ",source," ...", end='')
    d = feedparser.parse(source)
    twitter = None
    if debug: print(" ok.")
else:
    d = feedparser.parse('http://twitrss.me/twitter_user_to_rss/?user='+source)
    twitter = source

if debug: print("Processing", end='')

# Cut the source URL to avoid reposts when looking up the DB
source=source.split("/search/")[0]

for tweet in reversed(d.entries):
    # check if this tweet has been processed
    db.execute('SELECT * FROM tweets WHERE tweet = ? AND twitter = ?  and mastodon = ? and instance = ?', (tweet.id, source, mastodon_account, instance))  # noqa
    last = db.fetchone()
    dt = tweet.published_parsed
    age = datetime.now()-datetime(dt.tm_year, dt.tm_mon, dt.tm_mday, dt.tm_hour, dt.tm_min, dt.tm_sec)
    # process only unprocessed tweets less than 1 day old, after delay
    if last is None and age < timedelta(days=days) and age > timedelta(days=delay):
        if "token" in auth_type and mastodon_api is None:
            try:
                mastodon_api = Mastodon(client_id=app_client_id, client_secret=app_client_secret, access_token=app_access_token, api_base_url='https://'+instance)
            except:
                print("FATAL ERROR: Can't construct the mastodon client app")
                sys.exit(1)
        else:
            if mastodon_api is None:
                # Create application if it does not exist
                try:
                    Mastodon.create_app('tootbot', api_base_url='https://'+instance, to_file=auth_session)
                except:
                    print('FATAL ERROR: Failed to create app on instance '+instance)
                    sys.exit(1)
                try:
                    if debug: print("Trying to connect with ", instance+'.secret', " to ", 'https://'+instance, " ...", end='')
                    mastodon_api = Mastodon(client_id=auth_session, api_base_url='https://'+instance)
                    if debug: print(" ok.")
                except:
                    print("FATAL ERROR: Can't connect to Mastodon instance")
                    sys.exit(1)

                if debug: print("Login with email ", mastodon_email_account, " ...", end='')
                try:
                    mastodon_api.log_in(mastodon_email_account, mastodon_email_password, to_file=auth_session)
                    if debug: print(" ok.")
                except:
                    print("FATAL ERROR: First Login Failed !")
                    sys.exit(1)

        # construct the toot
        toot_body = tweet.title
        if debug: print("============================================================")
        if debug: print("[Posting]:",toot_body[0:60])
        if twitter and tweet.author.lower() != ('(@%s)' % twitter).lower():
            toot_body = ("RT https://twitter.com/%s\n" % tweet.author[2:-1]) + c
        toot_media = []

        # get the pictures... from twitter
        if twitter_pics_hosting in tweet.summary:
            for p in re.finditer(r"https://pbs.twimg.com/[^ \xa0\"]*", tweet.summary):
                try:
                    media = requests.get(p.group(0))
                    media_posted = mastodon_api.media_post(media.content, mime_type=media.headers.get('content-type'))
                    toot_media.append(media_posted['id'])
                except:
                    print("ERROR: Can't get media !")

        # get the pictures... from nitter
        if nitter_pics_hosting in tweet.summary:
            for p in re.finditer(r"https://nitter.net/pic/[^ \xa0\"]*", tweet.summary):
                try:
                    media = requests.get(p.group(0))
                    media_posted = mastodon_api.media_post(media.content, mime_type=media.headers.get('content-type'))
                    toot_media.append(media_posted['id'])
                except:
                    print("ERROR: Can't get media !")

        # replace short links by original URL
        m = re.search(r"http[^ \xa0]*", toot_body)
        if m is not None:
            l = m.group(0)
            try:
                r = requests.get(l, allow_redirects=False)
                if r.status_code in {301, 302}:
                    toot_body = toot_body.replace(l, r.headers.get('Location'))
            except:
                print("ERROR: Can't replace tweet's links !")

        # remove pic.twitter.com links
        m = re.search(r"pic.twitter.com[^ \xa0]*", toot_body)
        if m is not None:
            l = m.group(0)
            toot_body = toot_body.replace(l, ' ')

        # remove ellipsis
        toot_body = toot_body.replace('\xa0…', ' ')

        if twitter is None:
            toot_body = toot_body + '\n\nSource: ' + tweet.authors[0].name + ' ' + tweet.link

        if tags:
            if len(tags) > 2:
                toot_body = toot_body + '\n' + tags

        if toot_media is not None:
            try:
                toot = mastodon_api.status_post(toot_body, in_reply_to_id=None, media_ids=toot_media, sensitive=False, visibility='public', spoiler_text=None)
                if "id" in toot:
                    db.execute("INSERT INTO tweets VALUES ( ? , ? , ? , ? , ? )",(tweet.id, toot["id"], source, mastodon_account, instance))
                    sql.commit()
            except:
                print("ERROR: Can't toot! Sorry, skipping this one")
                if debug: print("Skipped toot:",c)
        sleep(posting_delay)
    else:
        if debug: print(".",end='')

if debug: print(" done.")
# end
