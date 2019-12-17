#!/usr/bin/env bash
#
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

# Sets ownership
echo "Setting ownership to files..."
[ -z "$MYUID" ] || usermod -u "$MYUID" "$USER"
chown -R --silent "$USER:$USER" "$APP_DIR"

# Starts lighter
export PATH="$PATH:$APP_DIR/.local/bin"
if [ -z "$@" ]; then
    echo "Starting lighter..."
    exec gosu "$USER" "lighter"
else
    echo "Starting $@..."
    exec gosu "$USER" "$@"
fi
