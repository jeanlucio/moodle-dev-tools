<?php
// This file is part of Moodle - https://moodle.org/
//
// Moodle is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// Moodle is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with Moodle.  If not, see <https://www.gnu.org/licenses/>.

/**
 * Ad-hoc probe copied into a container by core-updates-watch.py.
 *
 * Reuses Moodle's own \core\update\checker (the same class behind
 * Site administration > Notifications) to ask download.moodle.org for
 * available core updates, then keeps only the entry matching this
 * install's own branch. Printed as JSON on stdout so the calling
 * script never has to parse HTML.
 *
 * @package    tool_devtools
 * @copyright  2026 Jean Lúcio
 * @license    https://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

define('CLI_SCRIPT', true);
require(__DIR__ . '/config.php');

$checker = \core\update\checker::instance();
$checker->fetch();
$updates = $checker->get_update_info('core');

$version = null;
$release = null;
require($CFG->dirroot . '/version.php');

// The moodle_major_version(true) call already returns "X.Y" (e.g. "5.1"), matching
// the prefix of the $release strings the update checker returns (e.g. "5.1.5+ ...").
$mybranch = moodle_major_version(true);

$result = [
    'currentversion' => $version,
    'currentrelease' => $release,
    'branch' => $mybranch,
    'available' => null,
];

if ($updates && $mybranch !== false) {
    foreach ($updates as $update) {
        $samebranch = strpos($update->release, $mybranch . '.') === 0;
        if ($samebranch && $update->version > $version) {
            $result['available'] = [
                'version' => $update->version,
                'release' => $update->release,
                'download' => $update->download,
            ];
            break;
        }
    }
}

echo json_encode($result);
