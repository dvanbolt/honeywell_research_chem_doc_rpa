import logging
import time
import re
import csv
import sys
import inspect
import struct
import re
import json
import os
import pickle
import yaml
import signal
import typing as t
import pyodbc
import croniter
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from azure.identity import AzureCliCredential
from types import SimpleNamespace
from colorama import Fore,init
from zoneinfo import ZoneInfo


init(convert=True)

def get_module_class(class_name: str) -> type:
    for name, obj in inspect.getmembers(sys.modules[__name__]):
        if inspect.isclass(obj):
            if hasattr(obj, '__name__') and class_name == obj.__name__:
                return obj
    else:
        raise ValueError(f"Class '{class_name}' not found")

BatchRecord = dict[str, str | int | datetime | Path | None]

#Quick recursively deserializing config
class Config(SimpleNamespace):
    LOADERS = dict(
        json=(json.load, {}), yaml=(yaml.safe_load, {}), yml=(yaml.safe_load, {})
    )
    DUMPERS = dict(
        json=(json.dump, {"indent": 4}),
        yaml=(yaml.dump, {"sort_keys": False}),
        yml=(yaml.dump, {"sort_keys": False}),
    )

    ENV_VAR_PATTERN = re.compile(r"\{\{\s*env_var\(['\"](.+?)['\"]\)")

    @classmethod
    def load(cls, path: Path | str):
        path = Path(path)
        if path.exists():
            ld = cls.LOADERS.get(path.suffix[1:])
            if ld:
                loader, kwargs = ld
                with open(path, "r") as f:
                    data = loader(f, **kwargs)
                return cls.from_dict(data | {"__path__": path})
            else:
                raise NotImplementedError(path.suffix)
        raise ValueError(f"Path '{path}' does not exist")

    def __getitem__(self, key: str) -> t.Any:
        return self.__dict__[key]

    def items(self):
        return self.__dict__.items()

    def save(self):
        path = self.__path__
        if path.exists():
            dp = self.DUMPERS.get(path.suffix[1:])
            if dp:
                dumper, kwargs = dp
                data = self.__dict__
                data.pop("__path__")
                with open(path, "w") as f:
                    dumper(data, f, **kwargs)
                return path
            else:
                raise NotImplementedError(path.suffix)
        else:
            raise ValueError(f"Path '{path}' does not exist")


    @classmethod
    def from_dict(cls, d: dict):
        def _r(n,o: t.Any):
            if isinstance(o, dict):
                return cls(**{k: _r(k,v) for (k, v) in o.items()})
            elif isinstance(o, (list, tuple, set)):
                return [_r(n,v) for v in o]
            elif isinstance(o,str):
                env_var_match = re.search(cls.ENV_VAR_PATTERN, o)
                if env_var_match:
                    env_var_name = env_var_match.group(1)
                    try:
                        return os.environ.get(env_var_name)
                    except KeyError:
                        raise ValueError(f"Could not resolve environment variable '{env_var_name}'")
                return o
            else:
                return o

        inst = cls(**{key: _r(key,value) for (key, value) in d.items()})
        inst.resolve_paths()
        return inst

    def resolve_paths(self, parent_path: t.Optional[Path] = None):
        if not parent_path:
            parent_path = self.__path__.parent

        def _r(o: t.Any):
            if isinstance(o, dict):
                return {k: _r(v) for (k, v) in o.items()}
            if isinstance(o, self.__class__):
                return self.__class__(**_r(o.__dict__))
            elif isinstance(o, (list, tuple, set)):
                return [_r(v) for v in o]
            elif isinstance(o, str):
                o = Path(o)
                if not o.is_absolute():
                    o = parent_path / str(o)
                return o
            elif o is None:
                return o
            else:
                raise TypeError(type(o))

        if hasattr(self, "paths"):
            self.__dict__["paths"] = _r(self.__dict__["paths"])
        return self

@dataclass
class SQLConfig:
    server: str
    name: str | None = None
    user: str | None = None
    database: str | None = None
    password: str | None = None
    driver: str = "ODBC Driver 17 for SQL Server"
    trusted_connection: bool | None = None
    authentication: str | None = None
    port: int | None = None

    @property
    def conn_str(self) -> str:
        parts = ["DRIVER=" + self.driver, "SERVER=" + self.server]
        if self.port:
            parts.append(f"PORT={self.port}")
        if self.user is not None:
            parts.append("UID=" + self.user)
            if self.password:
                parts.append("PWD=" + self.password)
        if self.trusted_connection:
            parts.append("TRUSTED_CONNECTION=yes")

        if self.database:
            parts.append(f"DATABASE={self.database}")
        if self.authentication:
            parts.append(f"AUTHENTICATION={self.authentication}")
            parts.append(f"autocommit=yes")
        else:
            parts.append(f"TrustServerCertificate=yes")
        conn_str = ";".join(parts) + ";"
        return conn_str

    @property
    def full_name(self) -> str:
        return f"{self.name} ({self.server})"

class Schedule:
    def __init__(self,config:SimpleNamespace):
        self.config = config
        self._localized_tz, self.cron = self._parse_cron_str(config.cron)
        self._next_run: t.Optional[datetime] = self.get_next()
        self.enabled = self.config.enabled

    @staticmethod
    def _parse_cron_str(value)->tuple[t.Optional[ZoneInfo], t.Optional[str]]:
        if value:
            parts = value.split(" ")
            if len(parts) == 6:
                tz_str = parts[-1]
                tz = ZoneInfo(tz_str)
                _localized_tz = tz
                _cron = " ".join(value.split(" ")[:5])
            else:
                _cron = value
                _localized_tz = None
            return _localized_tz, _cron

    @property
    def next_run(self) -> t.Optional[datetime]:
        return self._next_run

    def complete(self) -> None:
        self._next_run: datetime = self.get_next()

    def get_next(self):
        if self.cron and self.config.enabled:
            return croniter.croniter(
                self.cron, datetime.now().astimezone(self._localized_tz)
            ).get_next(datetime)
        elif not self.cron:
            raise ValueError("No cron")
        elif not self.config.enabled:
            raise ValueError("Not enabled")

    @property
    def time_to_next_run(self) -> t.Optional[timedelta]:
        if self.next_run:
            return self.next_run - datetime.now().astimezone(self._localized_tz)

class DocCache(ABC):
    __PROPS__ = []
    def __init__(self, config:Config):
        self.config = config

    @abstractmethod
    def add_batch(self, item: str, catalog: str, batch: str) -> BatchRecord:...

    @abstractmethod
    def update_batch(self, catalog_number: str, batch_number: str, file_path: t.Optional[Path]) -> BatchRecord:...

    @abstractmethod
    def update_batches(self, batches: dict[tuple[str, str], Path | None]) -> list[BatchRecord]:...

    @abstractmethod
    def refresh(self, batches: list[dict]) -> tuple[list[BatchRecord], list[BatchRecord]]:...

    @abstractmethod
    def unresolved_keys(self, max_attempts: int = 0, selector: t.Optional[t.Callable] = None, **kwargs) -> list[BatchRecord]:...

    @abstractmethod
    def save(self) -> None:...

    @classmethod
    @abstractmethod
    def open(cls, path)->t.Self:...

class LocalCache(DocCache,dict):
    __PROPS__ = ['item_number',]
    def __init__(self,config):
        super().__init__(config)
        path = Path(self.config.path)
        if path.exists() and path.is_file():
            with open(path, 'rb') as f:
                self.__dict__ = pickle.loads(f.read())
        self._path = path

    def add_batch(self,item:str,catalog:str,batch:str) -> BatchRecord:
        key = (catalog.lower(),batch.lower())
        if key not in self:
            new_batch = {"item_number":item,"catalog_number":catalog,"batch_number":batch,"attempts":0,"last_attempt":None,"file_path":None}
            self[key] = new_batch
            return new_batch
        else:
            raise ValueError

    def update_batch(self,catalog_number:str,batch_number:str,file_path:t.Optional[Path]) -> BatchRecord:
        key = (catalog_number.lower(),batch_number.lower())
        if key in self:
            row = self[key]
            row["last_attempt"] = datetime.now()
            row["attempts"] = row["attempts"] + 1
            row["file_path"] = file_path
            return row
        else:
            raise ValueError(f"key {key} not found")

    def update_batches(self, batches:dict[tuple[str,str],Path | None]) -> list[BatchRecord]:
        updated = []
        for k, v in batches.items():
            updated.append(self.update_batch(*k, v))
        return updated

    def refresh(self,batches:list[dict]) -> tuple[list[BatchRecord],list[BatchRecord]]:
        added, updated = [],[]
        for row in batches:
            key = (row["catalog_number"].lower(),row["batch_number"].lower())
            if key in self:
                rec = self[key]
                for p in self.__PROPS__:
                    rec[p] = row[p]
                updated.append(rec)
            else:
                added.append(self.add_batch(*row.values()))
        return added,updated

    def unresolved_keys(self, max_attempts:int=0,selector:t.Optional[t.Callable] = None, **kwargs) -> list[BatchRecord]:
        return [k for (k,v) in self.items() if not v['file_path'] and (max_attempts == 0 or v['attempts'] <= max_attempts) and (selector is None or selector(v))]

    def save(self)->None:
        with open(self._path,'wb') as f:
            f.write(pickle.dumps(self.__dict__))

    @classmethod
    def open(cls,path)->t.Self:
        path = Path(path)
        if path.exists() and path.is_file():
            with open(path,'rb') as f:
                instance = pickle.loads(f.read())
        else:
            instance = cls()
        instance.__path__ = path
        return instance

    def to_csv(self,path:str|Path,encoding='utf-8',**kwargs)->None:
        if len(self)>0:
            with open(path, 'w', encoding=encoding,newline='') as output_file:
                dict_writer = csv.DictWriter(output_file, list(self.values())[0].keys())
                dict_writer.writeheader()
                dict_writer.writerows(self.values())

class SQLCache(DocCache):
    def __init__(self,config:Config):
        raise NotImplementedError()

class BatchSource(ABC):
    def __init__(self,config:Config):
        self.config = config

    @abstractmethod
    def get(self,limit:int=0)->list[dict]:...

class SQLBatchSource(BatchSource):
    def _get_sql_connection(self) -> pyodbc.Connection:
        credential = AzureCliCredential(
            tenant_id=self.config.tenant_id
        )
        databaseToken = credential.get_token("https://database.windows.net/")
        SQL_COPT_SS_ACCESS_TOKEN = 1256
        tokenb = bytes(databaseToken[0], "UTF-8")
        exptoken = b""
        for i in tokenb:
            exptoken += bytes({i})
            exptoken += bytes(1)
        tokenstruct = struct.pack("=i", len(exptoken)) + exptoken
        config = SQLConfig(
            server=self.config.server,
            port=1433,
            database=self.config.database,
        )

        connection_string = config.conn_str
        conn = pyodbc.connect(connection_string, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: tokenstruct})
        return conn

    def get(self,limit:int=0)->list[dict]:
        query = f"""
            select
            {"TOP "+str(limit) if limit else ""}
            CONCAT(UPPER(a.DataAreaId),'-',a.ItemId) [item_number]
            ,c.SEARCHNAME [catalog_number]
            ,b.INVENTBATCHID [batch_number]
            FROM InventTransOrigin a
            inner join InventTrans o on o.INVENTTRANSORIGIN = a.RECID and a.DataAreaId = o.DATAAREAID
            inner join dbo.InventDim b on o.INVENTDIMID = b.INVENTDIMID and a.DataAreaId = b.DATAAREAID and b.INVENTBATCHID is not null
            inner join [dbo].EcoResProduct c on c.DISPLAYPRODUCTNUMBER = CONCAT(UPPER(a.DataAreaId),'-',a.ItemId)
            where a.DataAreaId = 'hwna'"""

        connection = self._get_sql_connection()
        ret = []
        with connection.cursor() as cursor:
            cursor.execute(query)
            results = cursor.fetchall()
            header = [column[0] for column in cursor.description]
            for row in results:
                ret.append(dict(zip(header, row)))
        return ret

class CSVBatchSource(BatchSource):
    def get(self, limit: int = 0) -> list[dict]:
        data = []
        with open(self.config.path, 'r', encoding='utf-8-sig') as file:
            csv_reader = csv.DictReader(file)
            i = 0
            for row in csv_reader:
                data.append(row)
                if i == limit - 1 and limit:
                    break
                i += 1
        return data

class DocumentBot(ABC):
    def __init__(self, path: Path | str | None = None):
        if path is None:
            self.path = Path.cwd()
        else:
            self.path = Path(path)
        self.settings = Config.load(self.path / "config.yaml")
        self.config = self.settings.configs[self.settings.default_config]
        self.log = self._build_log(self.config.log)
        self.batch_source = self._load_class_with_config(self.config.batch_source)
        self.cache = self._load_class_with_config(self.config.cache)
        self.schedule = Schedule(self.config.schedule)

    def _load_class_with_config(self,config:Config):
        return get_module_class(config.type)(config)

    def _build_log(self,config:Config)->logging.Logger:
        COLOR_MAPPING = dict(DEBUG=Fore.LIGHTMAGENTA_EX,
                             INFO=Fore.LIGHTBLUE_EX,
                             WARNING=Fore.YELLOW,
                             ERROR=Fore.LIGHTRED_EX,
                             CRITICAL=Fore.RED)

        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler()
        if config.file:
            cp = Path(config.file_path)
            if not cp.parent.exists():
                cp.parent.mkdir(parents=True)
            file_handler = logging.FileHandler(config.file_path)
            if isinstance(config.file_level,int):
                file_handler.setLevel(config.file_level)
            elif isinstance(config.file_level,str):
                file_handler.setLevel(getattr(logging,config.file_level.upper()))
            file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        if config.console:
            if isinstance(config.console_level,int):
                console_handler.setLevel(config.console_level)
            elif isinstance(config.console_level,str):
                console_handler.setLevel(getattr(logging,config.console_level.upper()))
            console_handler.setLevel(logging.DEBUG)
            console_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(funcName)s - %(levelname_colored)s - %(message)s')
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
        else:
            console_handler.setLevel(logging.CRITICAL + 1)

        default_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = default_factory(*args, **kwargs)
            record.levelname_colored = COLOR_MAPPING[record.levelname] + record.levelname + Fore.RESET
            return record

        logging.setLogRecordFactory(record_factory)
        return logger

    def run(self,service:bool=False,**kwargs):
        if not service:
            return self.run_once(**kwargs)
        else:
            signal.signal(signal.SIGINT, self.interrupt_handler)
            tick = 0
            while True:
                if datetime.now(timezone.utc) >= self.schedule.next_run:
                    self.log.info(f"Triggering scheduled run")
                    self.run_once(**kwargs)
                    self.schedule.complete()
                else:
                    self.log.debug(f"Schedule up to date. Next run in {self.schedule.time_to_next_run}")

                tick += 1
                time.sleep(self.config.schedule.polling_interval)



    @abstractmethod
    def run_once(self, **kwargs)->None:...

    @abstractmethod
    def interrupt_handler(self, signo, frame)->None:...

class HoneywellCoXDocumentBot(DocumentBot):
    def run_once(self,**kwargs):
        default_kwargs = dict(self.config.run.items())
        run_kwargs = default_kwargs | kwargs
        if run_kwargs['refresh_cache']:
            required_batches = self.batch_source.get(limit = run_kwargs['limit'])
            added,updated = self.cache.refresh(required_batches)
        batches_to_resolve = self.cache.unresolved_keys(max_attempts=run_kwargs['max_attempts'])
        resolved_batches = self._get_documents(batches=batches_to_resolve,
                                                            save_dir=self.config.document_path,
                                                            doc_type=self.config.document)

        self.cache.update_batches(resolved_batches)
        if run_kwargs['print_cache']:...
            #make a cute printing method

        if run_kwargs['save_cache']:
            self.cache.save()


    def _get_documents(self,
                                    batches:list[tuple[str,str]],
                                    save_dir:str | Path,
                                    doc_type:str) -> dict[tuple[str,str],Path|None]:
        def _trim_id(s:str):
            s = s.lower().strip()
            match = re.search(r"^[a-z0-9]+", s)
            if match:
                return match.group(0)
            else:
                raise ValueError(str(s))

        def parse_batch_key(bk:tuple[str,str] | Path)->tuple[str,str]:
            if isinstance(bk, tuple):
                return tuple(map(_trim_id, bk))
            elif isinstance(bk, Path):
                b,c = bk.stem.split('_')
                return _trim_id(c), _trim_id(b)
            else:
                raise TypeError(bk)

        output:dict[tuple[str,str],Path|None] = {}
        root_url = f"https://lab.honeywell.com/en/{doc_type}?orderdirect=true"
        if doc_type not in ('coo','coa'):
            raise ValueError('doc_type must be coa/coo')
        target_dir = Path(save_dir).resolve()
        if not target_dir.exists():
            target_dir.mkdir(parents=True)

        existing_docs:dict[tuple[str,str],Path] = {parse_batch_key(file):file for file in target_dir.iterdir() if file.is_file() and file.suffix == '.pdf'}

        download_timeout = 5
        options = Options()
        prefs = {"download.default_directory":str(target_dir),
                 "plugins.always_open_pdf_externally": True,
                 }
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--window-size=2496,1664")
        options.add_argument("--start-maximized")


        driver_initialized = False
        exception = False
        try:
            for batch in batches:
                batch_key = parse_batch_key(batch)
                existing_doc = existing_docs.get(batch_key)
                if existing_doc:
                    output[batch] = existing_doc
                    self.log.info(f"Already have {doc_type} for {batch}. key = {batch_key}")
                else:
                    self.log.info(f"Downloading {doc_type} for {batch}. key = {batch_key}")
                    if not driver_initialized:
                        # options.add_argument('--headless=new')
                        driver = webdriver.Chrome(options=options)
                        #driver.minimize_window()
                        driver.get(root_url)
                        wait = WebDriverWait(driver, 5)

                        # Wait for and fill in the product number field
                        next_button = wait.until(EC.presence_of_element_located(
                            (By.ID, "countryNavigate")
                        ))
                        next_button.click()

                        #north_america = wait.until(EC.presence_of_element_located(
                        north_america = wait.until(EC.element_to_be_clickable(
                            (By.XPATH, "//a[contains(.//span, 'North America')]")
                        ))
                        #time.sleep(3)
                        north_america.click()
                        usa = wait.until(EC.element_to_be_clickable(
                            (By.XPATH, "//a[.//img[contains( @ src, 'usa.gif')]]")
                        ))
                        driver.execute_script("arguments[0].click();", usa)
                        # on the coa page now
                        driver_initialized = True

                    product_number, batch_number = batch_key
                    catalog_entry = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Enter product number']")))
                    catalog_entry.send_keys(product_number)
                    if doc_type == 'coa':
                        batch_tag = "//input[@placeholder='Enter lot/batch number (Preferred if available)']"
                    elif doc_type == 'coo':
                        batch_tag = "//input[@placeholder='Enter lot/batch number']"
                    batch_entry = driver.find_element(By.XPATH, batch_tag)
                    batch_entry.send_keys(batch_number)
                    batch_entry.send_keys(Keys.ENTER)
                    i, downloaded = 0, False
                    try:
                        #download_link = wait.until(EC.presence_of_element_located((By.CLASS_NAME, "dwn-coo-coa")))
                        download_link = wait.until(EC.presence_of_element_located((By.XPATH, f"//a[@class='dwn-coo-coa'][@data-lot-no='{batch_number.upper()}']")))
                    except TimeoutException:
                        try:
                            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "no-total-result")))
                            output[batch] = None
                        except TimeoutException:
                            raise Exception("Could not find a document or identify 'not found' messages.")
                    else:
                        file_prop = driver.execute_script(
                            'var items = {}; for (index = 0; index < arguments[0].attributes.length; ++index) { items[arguments[0].attributes[index].name] = arguments[0].attributes[index].value }; return items;',
                            download_link)
                        file_name = file_prop.get('data-file-name')
                        download_link.send_keys(Keys.ENTER)
                        expected_path = Path(target_dir) / file_name
                        while i <= download_timeout:
                            if expected_path.exists():
                                downloaded = True
                                break
                            else:
                                time.sleep(1)
                                i+=1
                        if downloaded:
                            bk = parse_batch_key(expected_path)
                            output[batch] = expected_path
                            existing_docs[bk] = expected_path
                            #print(f"Downloaded doc for {batch} with key {bk}")
                        else:
                            raise Exception("Download timed out.")
                    finally:
                        for i in range(len(product_number)):
                            catalog_entry.send_keys(Keys.BACKSPACE)
                            #time.sleep(0.05)
                        for i in range(len(batch_number)):
                            batch_entry.send_keys(Keys.BACKSPACE)
                            #time.sleep(0.05)

                        #not used but backup for fixing the downloading of the previous link
                        if downloaded and 1==2:
                            catalog_entry.send_keys("XX")
                            catalog_entry.send_keys(Keys.ENTER)
                            wait.until(EC.presence_of_element_located((By.XPATH,"//span[@class='no-result-key'][contains(text(), 'XXX')]")))
                            catalog_entry.send_keys(Keys.BACKSPACE)
                            catalog_entry.send_keys(Keys.BACKSPACE)




        except Exception as e:
            exception = True
            self.log.critical(e, exc_info=True)
        if driver_initialized:
            if exception and self.config.debug:
                i = input(f"Press any key to exit.")
            driver.quit()
        return output


    def interrupt_handler(self, signo, frame)->None:
        if input("Keyboard interrupt.\nSave cache? (y/n)->") == "y":
            self.cache.save()
        exit("bye")

if __name__ == '__main__':
    bot = HoneywellCoXDocumentBot()
    #bot.run(service=True)
    bot.run()
