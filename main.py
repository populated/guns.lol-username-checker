from enum import Enum
from typing import Final, ClassVar, Optional, Any, Dict, List
from dataclasses import dataclass, field
from itertools import product
import threading
import random
import string
import signal
import sys
import time
import re

from rich.progress import Progress, SpinnerColumn, TextColumn
from bs4 import BeautifulSoup, Tag
from rich.console import Console
from curl_cffi import requests
from rich.theme import Theme
from rich.text import Text
from rich.panel import Panel
from rich.live import Live
import orjson


__version__: Final = "1.0.0"
__author__: Final = "populated"
_shutdown: threading.Event = threading.Event()


class Status(Enum):
    BANNED = "banned"
    AVAILABLE = "available"
    TAKEN = "taken"


@dataclass
class Result:
    username: str
    status: Status
    timestamp: float = field(default_factory=time.time)


@dataclass
class Config:
    browser: str = "safari_ios"
    timeout: int = 10
    retries: int = 3
    delay: float = 1.0
    use_proxy: bool = False
    generator: Optional[Dict[str, Any]] = None
    
    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        with open(path, "rb") as f:
            data = orjson.loads(f.read())
        
        app = data.get("app", {})

        return cls(
            browser=app.get("browser", "safari_ios"),
            timeout=app.get("timeout", 10),
            retries=app.get("retries", 3),
            delay=app.get("delay", 1.0),
            use_proxy=app.get("use_proxy", False),
            generator=data.get("generation"),
        )


class Logger:
    __slots__ = ("console", "min_level", "start")
    
    LEVELS: ClassVar[Dict[str, int]] = {
        "DEBUG": 10,
        "INFO": 20,
        "SUCCESS": 25,
        "WARNING": 30,
        "ERROR": 40,
    }

    def __init__(self, level: str = "INFO") -> None:
        self.console = Console(
            theme=Theme(
                {
                    "info": "bright_blue",
                    "warning": "bright_yellow",
                    "error": "bright_red",
                    "success": "bright_green",
                    "timestamp": "dim",
                    "highlight": "bright_cyan",
                    "muted": "dim",
                }
            )
        )
        self.min_level: int = self.LEVELS.get(level.upper(), 20)
        self.start: float = time.time()

    def _fmt(self, level: str, message: str) -> Text:
        ts = Text(f"[{time.strftime('%H:%M:%S.%f')[:-3]}]", style="timestamp")
        lvl = Text(f"{level:>7}", style=level.lower())
        return ts + Text(" ") + lvl + Text(" ") + Text(message, style="white")

    def info(self, msg: str) -> None:
        if self.LEVELS["INFO"] >= self.min_level:
            self.console.print(self._fmt("INFO", msg))

    def error(self, msg: str) -> None:
        if self.LEVELS["ERROR"] >= self.min_level:
            self.console.print(self._fmt("ERROR", msg))

    def warning(self, msg: str) -> None:
        if self.LEVELS["WARNING"] >= self.min_level:
            self.console.print(self._fmt("WARNING", msg))

    def success(self, msg: str) -> None:
        if self.LEVELS["SUCCESS"] >= self.min_level:
            self.console.print(self._fmt("SUCCESS", msg))

    def banner(self) -> None:
        banner = Text.assemble(
            ("guns.lol checker ", "highlight"),
            ("v", "muted"),
            (__version__, "success"),
            (" by ", "muted"),
            (__author__, "info"),
        )
        self.console.print(Panel(banner, border_style="bright_blue", expand=False))


class ProxyPool:
    __slots__ = ("raw", "idx")
    _regex = re.compile(r"^(?:\w+:\/\/)?(?:[^:@]+:[^@]+@)?[^:@]+:\d+$")

    def __init__(self, path: str = "proxies.txt") -> None:
        self.raw: List[str] = []
        self.idx: int = 0
        
        self._load(path)

    def _load(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if (line := line.strip()) and not line.startswith("#") and self._regex.match(line):
                        self.raw.append(line)
        except FileNotFoundError:
            pass

    def _fmt(self, proxy: str) -> Dict[str, str]:
        proto = proxy.split("://")[0] if "://" in proxy else "http"

        return {
            "http": f"{proto}://{proxy}" if "://" not in proxy else proxy,
            "https": f"{proto}://{proxy}" if "://" not in proxy else proxy,
        }

    def next(self) -> Optional[Dict[str, str]]:
        if not self.raw:
            return None
        
        self.idx = (self.idx + 1) % len(self.raw)
        return self._fmt(self.raw[self.idx])

    def random(self) -> Optional[Dict[str, str]]:
        return self._fmt(random.choice(self.raw)) if self.raw else None

    def count(self) -> int:
        return len(self.raw)


class Generator:
    __slots__ = ("min_len", "max_len", "charset", "count")
    
    def __init__(self, config: Optional[Dict[str, Any]]) -> None:
        cfg: Optional[Dict[str, Any]]  = config or {}
        self.min_len: int = cfg.get("min", 6)
        self.max_len: int = cfg.get("max", 12)

        digits: int = cfg.get("digits", True)
        self.charset: str = string.ascii_lowercase + (string.digits if digits else "")
        
        cnt: int = cfg.get("count", 50)
        self.count: int = 0 if cnt == "max" and self.max_len <= 4 else int(cnt)

    def generate(self) -> List[str]:
        return self._all() if self.count == 0 else self._random(self.count)

    def _all(self) -> List[str]:
        results: List[str] = []

        for length in range(self.min_len, self.max_len + 1):
            for combo in product(self.charset, repeat=length):
                results.append("".join(combo))

        return results

    def _random(self, count: int) -> List[str]:
        return [
            "".join(random.choices(self.charset, k=random.randint(self.min_len, self.max_len)))
            for _ in range(count)
        ]


class Checker:
    __slots__ = ("cfg", "log", "proxies", "sess")
    URL: ClassVar[str] = "https://guns.lol/{}"
    BAN: ClassVar[str] = "This user has been banned from"
    AVAILABLE: ClassVar[str] = "Username not found"

    def __init__(
        self,
        cfg: Config,
        log: Logger,
        proxies: Optional[ProxyPool] = None,
    ) -> None:
        self.cfg: Config = cfg
        self.log: Logger = log
        self.proxies: Optional[ProxyPool] = proxies

        self.sess: requests.Session = requests.Session()
        self._setup()

    def _setup(self) -> None:
        self.sess.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })

    def _proxy(self) -> Optional[Dict[str, str]]:
        return self.proxies.next() if self.proxies and self.cfg.use_proxy else None

    def fetch(self, username: str) -> str:
        url = self.URL.format(username)
        attempt = 0

        while attempt < self.cfg.retries and not _shutdown.is_set():
            attempt += 1
            proxy = self._proxy()

            try:
                resp = self.sess.get(
                    url,
                    impersonate=self.cfg.browser,
                    timeout=self.cfg.timeout,
                    proxies=proxy,
                )
                resp.raise_for_status()
                return resp.text

            except Exception as e:
                self.log.error(f"Attempt {attempt} failed for {username}: {e}")
                
                if attempt >= self.cfg.retries:
                    raise
                
                time.sleep(self.cfg.delay * attempt)

        return ""

    def parse(self, html: str) -> Status:
        soup = BeautifulSoup(html, "html.parser")
        
        if not (h1 := soup.find("h1")):
            return Status.TAKEN

        h1 = h1.get_text(strip=True)

        if self.BAN in h1:
            return Status.BANNED

        if self.AVAILABLE in h1:
            h3 = soup.find("h3")
            
            if h3 and "Claim this username" in h3.get_text(strip=True):
                return Status.AVAILABLE

        return Status.TAKEN

    def check(self, username: str) -> Result:
        html = self.fetch(username)
        status = self.parse(html)
        
        method = (
            self.log.success
            if status is Status.AVAILABLE
            else self.log.error if status is Status.TAKEN else self.log.warning
        )
        method(f"{username} is {status.value}")

        return Result(username=username, status=status)

    def batch(self, usernames: List[str]) -> List[Result]:
        results: List[Result] = []
        
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.log.console,
        )

        with Live(progress, console=self.log.console, refresh_per_second=10):
            task = progress.add_task("Checking usernames...", total=len(usernames))

            for username in usernames:
                if _shutdown.is_set():
                    self.log.warning("Shutdown requested, stopping...")
                    break

                try:
                    results.append(self.check(username))
                except KeyboardInterrupt:
                    self.log.warning("Interrupted by user")
                    break
                except Exception as e:
                    self.log.error(f"Failed to check {username}: {e}")
                    results.append(Result(username=username, status=Status.TAKEN))
                finally:
                    progress.advance(task)

        return results


def shutdown(sig, frame):
    _shutdown.set()


def main() -> int:
    signal.signal(signal.SIGINT, shutdown)

    try:
        cfg: Config = Config.load()
        log: Logger = Logger(level="INFO")
        log.banner()

        proxies: Optional[ProxyPool] = ProxyPool() if cfg.use_proxy else None

        if proxies and proxies.count():
            log.info(f"Loaded {proxies.count()} proxies")

        generator: Optional[Generator] = Generator(cfg.generator) if cfg.generator else None
        
        if not generator:
            log.error("No generator config found")
            return 1
            
        usernames = generator.generate()
        log.info(f"Checking {len(usernames)} usernames")

        checker: Checker = Checker(cfg=cfg, log=log, proxies=proxies)
        results = checker.batch(usernames)

        available = [r.username for r in results if r.status is Status.AVAILABLE]

        if available:
            log.success(f"Available: {', '.join(available)}")
        else:
            log.warning(f"None available out of {len(usernames)} checked")

        return 0

    except KeyboardInterrupt:
        return 130

    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
