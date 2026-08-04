<?php
// phpcs:ignoreFile
// PHPUnit 11 extension for moodle-query-baseline: counts $DB queries fired by each test,
// via $DB->perf_get_queries() (lib/dml/moodle_database.php), a cumulative counter since
// process boot that Moodle already maintains — no test file needs to change.
//
// This file is itself the phpunit.xml ROOT `bootstrap` attribute (not the `<extensions>`
// one): PHPUnit's <extensions><bootstrap class="..."/> only checks class_exists() on the
// class name, it never `require`s a file for you (confirmed against the installed
// schema/11.4.xsd — the bootstrapType complex type has no `file` attribute). So this file
// boots Moodle itself first, then declares the extension class that <extensions> refers to
// by name — one file doing both jobs instead of needing a second wrapper.

namespace moodle_dev_tools\phpunit;

use PHPUnit\Event\Test\Finished as TestFinished;
use PHPUnit\Event\Test\FinishedSubscriber as TestFinishedSubscriber;
use PHPUnit\Event\Test\Prepared;
use PHPUnit\Event\Test\PreparedSubscriber;
use PHPUnit\Event\TestRunner\Finished as RunnerFinished;
use PHPUnit\Event\TestRunner\FinishedSubscriber as RunnerFinishedSubscriber;
use PHPUnit\Runner\Extension\Extension;
use PHPUnit\Runner\Extension\Facade;
use PHPUnit\Runner\Extension\ParameterCollection;
use PHPUnit\TextUI\Configuration\Configuration;

require_once(getenv('MOODLE_BOOTSTRAP_PATH') ?: '/var/www/html/public/lib/phpunit/bootstrap.php');

/**
 * Shared mutable state between the three subscribers below. A plain object passed by
 * constructor injection, not static properties — avoids the visibility gymnastics of
 * reaching into an outer class's private statics from the anonymous-class alternative.
 */
final class query_collector {
    /** @var array<string,int> Snapshot of perf_get_queries() when each test started. */
    public array $startcounts = [];

    /** @var array<string,int> Query count fired by each test (delta, not cumulative). */
    public array $counts = [];
}

/**
 * Snapshots the cumulative query counter right before the test body runs. This lands after
 * setUp() (Prepared fires once PHPUnit has finished preparing the test for execution), so
 * fixture-creation cost is not attributed to the test — only what happens from here on.
 */
final class query_prepared_subscriber implements PreparedSubscriber {
    public function __construct(private readonly query_collector $collector) {
    }

    public function notify(Prepared $event): void {
        global $DB;
        $this->collector->startcounts[$event->test()->id()] = $DB->perf_get_queries();
    }
}

/**
 * Computes the delta at test end. Falls back to a fresh snapshot as the start point if
 * Prepared never fired for this id (defensive — should not happen in practice), so a
 * missing start reads as "0 queries" rather than a PHP notice on an undefined key.
 */
final class query_finished_subscriber implements TestFinishedSubscriber {
    public function __construct(private readonly query_collector $collector) {
    }

    public function notify(TestFinished $event): void {
        global $DB;
        $id = $event->test()->id();
        $start = $this->collector->startcounts[$id] ?? $DB->perf_get_queries();
        $this->collector->counts[$id] = $DB->perf_get_queries() - $start;
    }
}

/**
 * Flushes the accumulated per-test counts to disk once, at the very end of the whole run —
 * query_baseline.py reads this file back out after the container exits.
 */
final class query_runner_finished_subscriber implements RunnerFinishedSubscriber {
    public function __construct(
        private readonly query_collector $collector,
        private readonly string $outputpath
    ) {
    }

    public function notify(RunnerFinished $event): void {
        file_put_contents(
            $this->outputpath,
            json_encode($this->collector->counts, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)
        );
    }
}

/**
 * Registration entry point — the class named in phpunit.xml's <extensions><bootstrap class="...">.
 */
final class query_count_extension implements Extension {
    public function bootstrap(
        Configuration $configuration,
        Facade $facade,
        ParameterCollection $parameters
    ): void {
        $collector = new query_collector();
        $outputpath = getenv('MOODLE_QUERY_BASELINE_OUTPUT') ?: '/tmp/moodle-query-counts.json';

        $facade->registerSubscriber(new query_prepared_subscriber($collector));
        $facade->registerSubscriber(new query_finished_subscriber($collector));
        $facade->registerSubscriber(new query_runner_finished_subscriber($collector, $outputpath));
    }
}
