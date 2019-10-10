# Copyright (C) 2018 inbitcoin s.r.l.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

""" Securing: implementation secrets, macaroons creation and storage """

import sys

from concurrent.futures import TimeoutError as TimeoutErrFut, \
    ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from functools import wraps
from getpass import getpass
from logging import getLogger
from select import select
from time import time, sleep
from os import environ, path, remove, urandom

from lighter import settings as sett
from lighter.db import get_secret_from_db, init_db, is_db_ok, \
    save_mac_params_to_db, save_secret_to_db, save_token_to_db, \
    session_scope
from lighter.macaroons import get_baker, MACAROONS, MAC_VERSION
from lighter.utils import check_password, Crypter, FakeContext, \
    get_start_options, ScryptParams, str2bool, update_logger

LOGGER = getLogger(__name__)

DONE = False
NEW_DB = False

SEARCHING_ENTROPY = True
COLLECTING_INPUT = True

READ_LIST = [sys.stdin]

IDLE_MESSAGES = {
    1: {
        'msg': 'please keep generating entropy',
        'delay': 5
    },
    2: {
        'msg': 'more entropy, please',
        'delay': 15
    },
    3: {
        'msg': '...good things come to those who wait...',
        'delay': 30
    }
}

FIRST_WORK_TIME = None
IDLE_COUNTER = 1
IDLE_LAST_DELAY = 15


def _exit(message):
    """ Exits printing a message """
    if message:
        print(message)
    sys.exit(0)


def _handle_keyboardinterrupt(func):
    """ Handles KeyboardInterrupt """

    @wraps(func)
    def wrapper(*args, **kwargs):
        global COLLECTING_INPUT
        global SEARCHING_ENTROPY
        global DONE
        global NEW_DB
        try:
            func(*args, **kwargs)
        except KeyboardInterrupt:
            print()
            COLLECTING_INPUT = False
            SEARCHING_ENTROPY = False
            if not DONE and NEW_DB:
                _remove_files()
            _exit('\nKeyboard interrupt detected. Exiting...')

    return wrapper


def _consume_bytes(consumable, num):
    """ Consume num elements from a byte string and returns them """
    consumable = list(consumable)
    i = 0
    data = []
    while i < num:
        data.append(consumable.pop(0))
        i = i + 1
    return bytes(data)


def _remove_files():
    """ Removes database and macaroons """
    LOGGER.info('Removing database and macaroons')
    database = path.join(sett.DB_DIR, sett.DB_NAME)
    with suppress(FileNotFoundError):
        remove(database)
        for file_name in MACAROONS:
            macaroon_file = path.join(sett.MACAROONS_DIR, file_name)
            remove(macaroon_file)


def _get_eclair_secret():
    """ Gets eclair's password from stdin """
    data = getpass('Insert eclair password: ')
    return data.encode()


def _get_lnd_secret(macaroon_path):
    """ Gets lnd's macaroon from file """
    print('Reading lnd macaroon from the provided path...')
    with open(macaroon_path, 'rb') as file:
        return file.read()


def _set_eclair(secret):
    """ Handles storage of eclair's password """
    if not secret:
        return _get_eclair_secret()
    rm_sec = input("A password for eclair is already stored, "
                   "do you want to update it? [y/N] ")
    if str2bool(rm_sec):
        return _get_eclair_secret()


def _set_lnd(secret):
    """ Handles storage of lnd's macaroon """
    data = None
    activate_secret = 1
    macaroon_path = environ.get('LND_MAC_PATH')
    if not secret and not macaroon_path:
        print("You have not provided a path and there's no macaroon "
              "stored for lnd, assuming\nusage of lnd without macaroon")
        return None, 0
    if macaroon_path:
        data = _get_lnd_secret(macaroon_path)
        secret = None
    if secret:
        print('A macaroon for lnd is already stored')
        data = secret
    if secret or macaroon_path:
        res = input("Connect to lnd using its macaroon "
                    "(warning: insecure without)? [Y/n] ")
        if not str2bool(res, force_true=True):
            activate_secret = 0
    return data, activate_secret


def _encrypt_token(session, password, scrypt_params):
    """ Encrypts token and saves it into db to verify password correctness """
    derived_key = Crypter.gen_derived_key(password, scrypt_params)
    encrypted_token = Crypter.crypt(sett.ACCESS_TOKEN, derived_key)
    save_token_to_db(session, encrypted_token, scrypt_params.serialize())
    LOGGER.info('Encrypted token stored in the DB')


def _encrypt_secret(session, password, scrypt_params, sec_type, secret,
                    activate_secret):
    """ Encrypts implementation secret into db """
    derived_key = Crypter.gen_derived_key(password, scrypt_params)
    encrypted_secret = Crypter.crypt(secret, derived_key)
    save_secret_to_db(
        session, sett.IMPLEMENTATION, sec_type, activate_secret,
        encrypted_secret, scrypt_params.serialize())


def _recover_secrets(session, password):
    """ Recovers secrets from db, making sure password is correct """
    ecl_sec = lnd_sec = None
    try:
        ecl_sec, _, params = get_secret_from_db(session, 'eclair')
        if ecl_sec:
            ecl_params = ScryptParams('')
            ecl_params.deserialize(params)
            derived_key = Crypter.gen_derived_key(password, ecl_params)
            ecl_sec = Crypter.decrypt(FakeContext(), ecl_sec, derived_key)
        lnd_sec, _, params = get_secret_from_db(session, 'lnd')
        if lnd_sec:
            lnd_params = ScryptParams('')
            lnd_params.deserialize(params)
            derived_key = Crypter.gen_derived_key(password, lnd_params)
            lnd_sec = Crypter.decrypt(FakeContext(), lnd_sec, derived_key)
    except RuntimeError as err:
        _exit(err)
    return ecl_sec, lnd_sec


def _create_lightning_macaroons(session, password, scrypt_params):
    """ Creates macaroon files to use the LightningServicer """
    print('Creating macaroons...')
    sett.MAC_ROOT_KEY = Crypter.gen_derived_key(password, scrypt_params)
    save_mac_params_to_db(session, scrypt_params.serialize())
    baker = get_baker(sett.MAC_ROOT_KEY)
    for file_name, permitted_ops in MACAROONS.items():
        macaroon_file = path.join(sett.MACAROONS_DIR, file_name)
        expiration_time = datetime.now(tz=timezone.utc) + timedelta(days=365)
        caveats = None
        mac = baker.oven.macaroon(
            MAC_VERSION, expiration_time, caveats, permitted_ops)
        serialized_macaroon = mac.macaroon.serialize()
        with open(macaroon_file, 'wb') as file:
            file.write(serialized_macaroon.encode())
        LOGGER.info('%s written to %s', file_name, sett.MACAROONS_DIR)


def _get_entropy():
    """ Gets available entropy """
    with open('/proc/sys/kernel/random/entropy_avail', 'r') as entropy:
        entropy_available = int(entropy.read())
        return entropy_available


def _input():
    """ Gets input asynchronously """
    global COLLECTING_INPUT
    global READ_LIST
    global FIRST_WORK_TIME
    FIRST_WORK_TIME = time()
    while READ_LIST and COLLECTING_INPUT:
        ready = select(READ_LIST, [], [], 0.2)[0]
        if not ready and COLLECTING_INPUT:
            _idle()
        else:
            for file in ready:
                line = file.readline()
                if not line: # EOF, remove file from input list
                    READ_LIST.remove(file)
                elif line.rstrip():
                    return line.rstrip()


def _idle():
    """ During input idling prints periodic messages """
    global FIRST_WORK_TIME
    global IDLE_COUNTER
    global IDLE_MESSAGES
    if time() - FIRST_WORK_TIME > IDLE_MESSAGES[IDLE_COUNTER]['delay']:
        print(IDLE_MESSAGES[IDLE_COUNTER]['msg'])
        if IDLE_COUNTER == 3:
            IDLE_MESSAGES[IDLE_COUNTER]['delay'] = \
                IDLE_MESSAGES[IDLE_COUNTER]['delay'] + 15
        else:
            IDLE_COUNTER = IDLE_COUNTER + 1


def _read(source, num_bytes):
    """ Gets entropy from /dev/random asynchronously """
    global SEARCHING_ENTROPY
    try:
        while _get_entropy() < num_bytes * 8 * 1.2:
            if not SEARCHING_ENTROPY:
                return
            sleep(1)
        random_bytes = source.read(num_bytes)
        return random_bytes
    except Exception:
        use_urand = input("Cannot retrieve available entropy, do you want to "
                          "use whatever your os provides\nto python's "
                          "os.urandom? [Y/n] ")
        if str2bool(use_urand, force_true=True):
            return
        _exit("No way to retrieve the amount of available entropy")


def _gen_random_data(num_bytes):
    """ Generates random data of key_len length """
    global COLLECTING_INPUT
    global SEARCHING_ENTROPY
    if sett.ENTROPY_BLOCKING:
        print('Trying to collect entropy...')
        try:
            source = open("/dev/random", "rb")
            executor = ThreadPoolExecutor(max_workers=2)
            future_input = None
            future = executor.submit(_read, source, num_bytes)
            data = future.result(timeout=1)
            if data:
                return data
        except FileNotFoundError:
            use_urand = input("The blocking '/dev/random' entropy source is "
                              "not available, do you want to use\nwhatever "
                              "your os provides to python's os.urandom? "
                              "[Y/n] ")
            if str2bool(use_urand, force_true=True):
                return urandom(num_bytes)
            _exit("No Random Numbers Generator available")
        except TimeoutErrFut:
            print("This call might take long depending on the "
                  "available entropy.\nIf this happens you can:\n"
                  " - type randomly on the keyboard or move the mouse\n"
                  " - install entropy collecting tools like haveged\n"
                  " - install a hardware TRNG\n"
                  " - type 'unsafe' and press enter to use a non-blocking "
                  "entropy source; this choice will NOT be remembered for "
                  "later\n")
            while future.running():
                if not future_input:
                    future_input = executor.submit(_input)
                if future_input.done():
                    if future_input.result() == 'unsafe':
                        SEARCHING_ENTROPY = False
                        COLLECTING_INPUT = False
                        return urandom(num_bytes)
                    future_input = None
                sleep(1)
            COLLECTING_INPUT = False
            print("\nEnough entropy was collected, thanks for waiting")
            result = future.result()
            if result:
                return result
        finally:
            if source:
                source.close()
            if executor:
                executor.shutdown(wait=False)
    return urandom(num_bytes)


def _gen_password(seed):
    """ Generates a safe random password from a 64-char alphabet """
    # base58 charset plus some symbols up to 64
    alpha = r'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz+/\&-_'
    assert 256 % len(alpha) is 0
    return ''.join(alpha[i % len(alpha)] for i in seed)


def configure_db(session, new):
    """ Configures a new or existing database """
    ecl_sec = lnd_sec = None
    if new:
        print('Lighter is about to ask for a new password! As humans are '
              'really bad at\ngenerating entropy, we suggest using a password '
              'manager to generate and store\nthe password on your behalf. '
              'Refer to doc/security.md for more details.')
        gen_psw = input('Do you want Lighter to generate a safe random '
                        'password for you? (new password\nwill be printed to '
                        'stdout) [Y/n] ')
        salts_len = sett.SALT_LEN * 3  # for token, secret and macaroon
        if str2bool(gen_psw, force_true=True):
            seed = _gen_random_data(sett.PASSWORD_LEN + salts_len)
            password = _gen_password(_consume_bytes(seed, sett.PASSWORD_LEN))
            print("Here is your new password:")
            print(password)
        else:
            seed = _gen_random_data(salts_len)
            password = getpass('Insert a safe password for Lighter: ')
        password_check = getpass('Save the password and then enter it '
                                 'for verification: ')
        if password != password_check:
            _exit("Passwords do not match")
        scrypt_params = ScryptParams(_consume_bytes(seed, sett.SALT_LEN))
        _encrypt_token(session, password, scrypt_params)
    else:
        if not is_db_ok(session):
            _exit('Detected an incomplete configuration. Delete database.')
        password = getpass("Insert Lighter's password: ")
        try:
            check_password(FakeContext(), session, password)
        except RuntimeError:
            _exit('')
        ecl_sec, lnd_sec = _recover_secrets(session, password)
        seed = _gen_random_data(sett.SALT_LEN * 2)  # for macaroon and secret
    create_mac = input('Do you want to create macaroons (warning: generated '
                       'files should not be kept in\nthis host)? [y/N] ')
    if str2bool(create_mac):
        scrypt_params = ScryptParams(_consume_bytes(seed, sett.SALT_LEN))
        _create_lightning_macaroons(session, password, scrypt_params)
    data = crypt_data = None
    activate_secret = 1
    sec_type = ''
    if sett.IMPLEMENTATION == 'eclair':
        data = _set_eclair(ecl_sec)
        sec_type = 'password'
    if sett.IMPLEMENTATION == 'lnd':
        data, activate_secret = _set_lnd(lnd_sec)
        sec_type = 'macaroon'
    if data:  # user gave us data to encrypt and save
        scrypt_params = ScryptParams(_consume_bytes(seed, sett.SALT_LEN))
        _encrypt_secret(
            session, password, scrypt_params, sec_type, data, activate_secret)


@_handle_keyboardinterrupt
def secure():
    """ Handles Lighter and implementation secrets """
    update_logger()
    get_start_options()
    no_db = environ.get('NO_DB')
    rm_db = environ.get('RM_DB')
    if rm_db:
        _remove_files()
    global NEW_DB
    NEW_DB = no_db or rm_db
    init_db(new_db=NEW_DB)
    with session_scope(FakeContext()) as session:
        configure_db(session, NEW_DB)
    global DONE
    DONE = True
    _exit('All done!')
