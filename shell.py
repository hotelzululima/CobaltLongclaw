#!/usr/bin/env python3

import argparse, random, select, string, subprocess, time, threading, sys, socket
import logging

import socks, stem, stem.control

from IPython.terminal.embed import InteractiveShellEmbed
import IPython
from traitlets import config


from Bot import Bot
import EventManager, IRCLineHandler

IRC_COLORS = ["02", "03", "04", "05", "06", "07", "08", "09",
    "10", "11", "12", "13"]

def read_proxy_list(filename):
    with open("proxy_list.txt") as f:
        plist = [line.split() for line in f.read().split("\n")]
        proxies = set()
        for line in plist:
            if not line: continue
            # 4 1.1.1.1:1234 RU -
            if ":" in line[1]:
                hostport = line[1].split(":")
                proxies.add((line[0], hostport[0], int(hostport[1])))
            # 4 1234 1.1.1.1 RU -
            else:
                proxies.add((line[0], line[2], int(line[1])))
    return proxies

LOG_LEVELS = {
    "trace": logging.DEBUG-1,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARN,
    "error": logging.ERROR,
    "critical": logging.CRITICAL
}

loggers = {}

def get_logger(name):
    global loggers
    if name in loggers:
        return loggers[name]

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    # This mode ensures that logs will be overwritten on each run
    handler = logging.FileHandler("logs/{}.log".format(name), mode="w")
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    loggers[name] = logger
    return logger

def log_message(name, message, level="info"):
    get_logger(name).log(LOG_LEVELS[level], message)

def new_circuit(tor_password, tor_port):
    log_message("proxy", "Acquiring new Tor circuit")
    with stem.control.Controller.from_port(port = tor_port
            ) as controller:
        controller.authenticate(tor_password)
        controller.signal(stem.Signal.NEWNYM)

def new_socket(protocol="5", host="localhost", port=9050):
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5 if protocol=="5" else socks.SOCKS4, host, port)
    s.settimeout(2.5)
    log_message("proxy", "SOCKS {} {}:{}".format(protocol, host, port))
    return s

# List of characters, minimum bound, maximum bound (inclusive)
def random_string(letterset, min, max):
    return "".join(random.choice(letterset) for a in range(min,max+1))

def rainbow_string(s):
    rainbow = ""
    for c in s:
        color = random.choice(IRC_COLORS)
        rainbow += "\x03%s,00%s" % (color, c)
    return rainbow

class IdentityProvider:
    # Tuple of nickname, username (or ident, if you prefer), and "real name"
    def new_identity(self):
        return ("CobaltLongclaw", "CobaltLongclaw", "CobaltLongclaw r2")

class RandomIdentityProvider(IdentityProvider):
    def new_identity(self):
        return (random_string(string.ascii_lowercase,2,11), random_string(string.ascii_lowercase,2,11),
            random_string(string.ascii_lowercase,2,11))

class AnimalIdentityProvider(IdentityProvider):
    former = ["Black", "White", "Grey", "Crimson", "Azure", "Aqua", "Violet", "Ash", "Blood", "Argent", "Copper", "Zinc", "Iron", "Gold", "Silver", "Chrome", "Cobalt"]
    latter = ["Wolf", "Eagle", "Fox", "Bear", "Scorpion", "Deer", "Swallow", "Goat", "Dragon"]
    def new_identity(self):
        name = random.choice(former) + random.choice(latter)
        return (name, name, name)

class LambdaIdentityProvider(IdentityProvider):
    def __init__(identity_function):
        self.new_identity = identity_function

class BotManager(object):
    def __init__(self):
        self.bots = {}
        self.running = True
        self.events = EventManager.EventHook(self)

        def set_status(event):
            event["bot"].last_status = event["command"]
        self.events.single("received/numeric").hook(set_status)

        self.poll = select.epoll()
        self._random_nicknames = []

    def run(self):
        while self.running:
            events = self.poll.poll(10)
            for fileno, event in events:
                bot = self.bots[fileno]
                if event & select.EPOLLIN:
                    lines = bot.read()
                    if not lines:
                        self.remove_bot(bot)
                    else:
                        for line in lines:
                            self.parse_line(line, bot)
                elif event & select.EPOLLOUT:
                    bot.send()
                    self.poll.modify(bot.fileno(),
                        select.EPOLLIN)
                elif event & select.EPOLLHUP:
                    self.remove_bot(bot)

            for bot in list(self.bots.values()):
                since_last_read = (
                    None if not bot.last_read else time.time(
                    )-bot.last_read)
                removed = False
                if since_last_read:
                    if since_last_read > 120:
                        self.remove_bot(bot)
                        removed = True
                    elif since_last_read > 30 and not bot.ping_sent:
                        bot.send_ping()
                if not removed and bot.waiting_send():
                    self.poll.modify(bot.fileno(),
                        select.EPOLLIN|select.EPOLLOUT)

    def parse_line(self, line, bot):
        if not line:
            return
        original_line = line
        prefix, final = None, None
        if line[0] == ":":
            prefix, line = line[1:].split(" ", 1)
        command, line = (line.split(" ", 1) + [""])[:2]
        if line[0] == ":":
            final, line = line[1:], ""
        elif " :" in line:
            line, final = line.split(" :", 1)
        args_split = line.split(" ") if line else []
        if final:
            args_split.append(final)
        IRCLineHandler.handle(original_line, prefix, command, args_split, final!=None, bot, self)

    def all(self, function, *args):
        for bot in list(self.bots.values()):
            function(bot, *args)

    def add_bot(self, bot):
        self.bots[bot.fileno()] = bot
        self.poll.register(bot.fileno(), select.EPOLLIN)

    def remove_bot(self, bot):
        self.poll.unregister(bot.fileno())
        del self.bots[bot.fileno()]

    def __len__(self):
        return len(self.bots)

    def start(self):
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()
        return self.thread

    def summary(self):
        return ", ".join([a.summary() for a in self.bots.values()])

class ClientFactory(object):
    def __init__(self, host, port, bot_count, tor_password,
            tor_port, proxies, use_tor=True):
        self.bot_manager = BotManager()
        self.host = host
        self.port = port
        self.bot_count = bot_count
        self.tor_password = tor_password
        self.tor_port = tor_port
        self.running = True
        self.thread = threading.Thread()
        self.connection_count = 0
        self.use_tor = use_tor
        self.proxies = proxies
        self.identity_provider = RandomIdentityProvider()

    def run(self):
        log_message("proxy", "ClientFactory thread started")

        while self.running:
            if len(self.bot_manager.bots) < self.bot_count:
                count = min(self.bot_count-len(self.bot_manager.bots),
                    1) # 3
                if self.use_tor:
                    new_circuit(self.tor_password, self.tor_port)
                sockets = []
                for n in range(count):
                    if self.use_tor:
                        sockets.append(new_socket())
                    else:
                        if self.proxies:
                            sockets.append(new_socket(*proxies.pop()))
                        else:
                            log_message("proxy", "SOCKS proxy list exhausted", "error")
                for socket in sockets:
                    try:
                        socket.connect((self.host, self.port))
                    except socks.ProxyError as e:
                        log_message("proxy", "SOCKS problem of some sort", "error")
                        continue
                    except Exception as e:
                        log_message("proxy", "Other socket issue", "error")
                        continue
                    bot = Bot(socket, *self.identity_provider.new_identity())
                    bot.identify()
                    self.connection_count += 1
                    self.bot_manager.add_bot(bot)
                time.sleep(5)
            else:
                time.sleep(1)

    def start(self):
        self.bot_manager.start()
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()
        return self.thread

    def __repr__(self):
        return "<{0}({1} {2}/{3} ({5}))> - [{4}]".format(self.__class__.__name__, "running"*self.thread.is_alive() or "stopped",
            len(self.bot_manager), self.bot_count, self.bot_manager.summary(), self.connection_count)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="host of the irc server")
    parser.add_argument("port", type=int, help="port of the irc server")
    parser.add_argument("-n", "--bot-count", type=int, default=100, help=
        "amount of bots to create")
    parser.add_argument("-tp", "--tor-password", help=
        "password to use to authenticate with Tor control")
    parser.add_argument("-tr", "--tor-port", type=int, help=
        "Tor's control port", default=9051)
    parser.add_argument("-t", "--use-tor", action="store_true", default=True)
    parser.add_argument("-p", "--proxy-list", help="List of SOCKS proxies to use")


    args = parser.parse_args()

    proxies = []
    if args.proxy_list:
        proxies = read_proxy_list(args.proxy_list)

    client_factory = ClientFactory(args.host, args.port,
        args.bot_count, args.tor_password, args.tor_port, proxies, args.use_tor)

    bot_manager = client_factory.bot_manager

    sys.argv = sys.argv[:1]
    c = config.Config()
    c.InteractiveShell.banner1 = "`client_factory`, `bot_manager`"
    ip = InteractiveShellEmbed(config=c, user_ns=locals())
    IPython.get_ipython = lambda: ip
    ip()
