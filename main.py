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

""" Lighter starting point """

import sys

from os import environ
from signal import signal, SIGTERM

from lighter.utils import handle_keyboardinterrupt, log_intro, log_outro, \
    update_logger
from lighter.lighter import start

environ["GRPC_SSL_CIPHER_SUITES"] = (
    "HIGH+ECDSA:"
    "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384")


def sigterm_handler(_signo, _stack_frame):
    """ Raises SystemExit(0) """
    log_outro()
    sys.exit(0)


signal(SIGTERM, sigterm_handler)


@handle_keyboardinterrupt
def main():
    update_logger()
    log_intro()
    start()
    log_outro()


if __name__ == '__main__':
    main()
