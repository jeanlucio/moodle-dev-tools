<?php
// phpcs:ignoreFile
// Defines PHP 8.4 property-hook token constants as fallbacks for PHP < 8.4.
// Required because phpcsutils 1.1+ references these as compile-time constant
// expressions. Without this file, PHPCS crashes with a fatal error on PHP 8.3.
defined('T_PUBLIC_SET') || define('T_PUBLIC_SET', 'T_PUBLIC_SET');
defined('T_PROTECTED_SET') || define('T_PROTECTED_SET', 'T_PROTECTED_SET');
defined('T_PRIVATE_SET') || define('T_PRIVATE_SET', 'T_PRIVATE_SET');
