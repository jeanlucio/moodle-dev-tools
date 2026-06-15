<?php
// phpcs:disable moodle.Files.MoodleInternal
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

// phpcs:disable moodle.Files.MoodleInternal
// Standalone CLI tool — not loaded by the Moodle bootstrap.

/**
 * Sorts $string[] keys alphabetically in Moodle lang files.
 *
 * Limitation: files containing multiline string values are skipped. Moodle
 * core uses multiline strings (legacy), but plugin lang files must not — long
 * content belongs in Mustache templates. If a file is skipped, fix the
 * multiline value first.
 *
 * Usage: php sortlang.php <plugin_dir>
 *
 * @copyright  2026 Jean Lúcio
 * @license    https://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

if (php_sapi_name() !== 'cli') {
    die("This script must be run from the command line.\n");
}

$plugindir = $argv[1] ?? null;

if (!$plugindir || !is_dir($plugindir . '/lang')) {
    die("Usage: php sortlang.php <plugin_dir>\n");
}

$langdir = rtrim($plugindir, '/\\') . '/lang';

echo "Scanning: $langdir\n\n";

$iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($langdir));
$fileschanged = 0;
$fileschecked = 0;

foreach ($iterator as $file) {
    if (!$file->isFile() || $file->getExtension() !== 'php') {
        continue;
    }

    $fileschecked++;
    $filepath = $file->getPathname();
    $originalcontent = file_get_contents($filepath);
    $lines = explode("\n", str_replace("\r\n", "\n", $originalcontent));

    // Files with multiline string values are not supported: the script reads
    // values line-by-line and would silently drop continuation lines. Moodle
    // core has legacy multiline strings, but plugin lang files must not — move
    // long content to a Mustache template and re-run.
    $hasmultiline = false;
    foreach ($lines as $line) {
        if (preg_match('/^\$string\[/', $line) && !preg_match('/;\s*$/', $line)) {
            $hasmultiline = true;
            break;
        }
    }

    $langfolder = basename($file->getPath());
    $filename = $file->getFilename();

    if ($hasmultiline) {
        echo "[SKIPPED]  $langfolder/$filename — multiline string value found"
            . " (move long content to a Mustache template first).\n";
        $fileschecked--;
        continue;
    }

    $header = [];
    $strings = [];
    $isheader = true;

    foreach ($lines as $line) {
        if (preg_match('/^\$string\[\'([^\']+)\'\]\s*=/', $line, $matches)) {
            $isheader = false;
            $strings[$matches[1]] = rtrim($line);
        } else if ($isheader) {
            $header[] = rtrim($line);
        }
    }

    $originalorder = array_keys($strings);
    ksort($strings);
    $neworder = array_keys($strings);

    $outofordercount = 0;
    foreach ($originalorder as $index => $key) {
        if ($key !== $neworder[$index]) {
            $outofordercount++;
        }
    }

    $stringcount = count($strings);

    while (count($header) > 0 && trim(end($header)) === '') {
        array_pop($header);
    }

    $output = implode("\n", $header) . "\n\n";
    if ($stringcount > 0) {
        $output .= implode("\n", $strings) . "\n";
    }

    if ($output !== $originalcontent) {
        file_put_contents($filepath, $output);
        $fileschanged++;

        if ($outofordercount > 0) {
            echo "[SORTED]   $langfolder/$filename — $stringcount strings"
                . " ($outofordercount reordered).\n";
        } else {
            echo "[FORMATTED] $langfolder/$filename — $stringcount strings"
                . " (whitespace fixed).\n";
        }
    } else {
        echo "[OK]       $langfolder/$filename — $stringcount strings already in order.\n";
    }
}

echo "\nDone: $fileschecked files checked, $fileschanged updated.\n";
