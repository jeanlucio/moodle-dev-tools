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
 * BYOK transport client for local_aihub.
 *
 * @package    local_aihub
 * @copyright  2026 Jean Lúcio
 * @license    https://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

namespace local_aihub\local;

/**
 * Resolves a BYOK key and generates text against the matching provider.
 *
 * Tier order is personal keys (when allowed) then site keys; within a tier the
 * provider order is Gemini then Groq then DeepSeek then OpenAI-compatible. On a
 * provider failure the next available one is tried. core_ai is never consulted here.
 *
 * @package    local_aihub
 * @copyright  2026 Jean Lúcio
 * @license    https://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class client {
    /** @var int HTTP request timeout in seconds. */
    const HTTP_TIMEOUT = 30;

    /**
     * Resolves a provider and generates text, tier-first (personal then site).
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param bool $jsonmode Whether to request structured JSON output.
     * @param int|null $userid User whose personal tier is tried first. Defaults to $USER->id.
     * @return array Keys: success (bool), data (string), provider (string), model (string),
     *               keysource (string), message (string), attempts (array).
     */
    public function generate_text(string $system, string $user, bool $jsonmode = false, ?int $userid = null): array {
        global $USER;

        $userid = $userid ?? (int) $USER->id;
        $lasterror = ['success' => false, 'data' => '', 'provider' => '', 'model' => '', 'message' => ''];

        // Every provider call is kept, not just the winning one. A failure that is
        // followed by a success would otherwise be overwritten and lost, which is
        // exactly the case that hides a permanently broken key from the log.
        $attempts = [];

        // Tier 1: personal keys (the user's own, opt-in).
        if (keys::personal_keys_allowed($userid)) {
            $result = $this->try_key_tier($system, $user, $jsonmode, true, $userid, $lasterror, $attempts);
            if ($result !== null) {
                return $result + ['attempts' => $attempts];
            }
        }

        // Tier 2: site keys (admin-wide).
        $result = $this->try_key_tier($system, $user, $jsonmode, false, $userid, $lasterror, $attempts);
        if ($result !== null) {
            return $result + ['attempts' => $attempts];
        }

        return $lasterror + ['attempts' => $attempts];
    }

    /**
     * Tries Gemini then Groq then DeepSeek then OpenAI for one key tier (personal or site).
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param bool $jsonmode Whether to request structured JSON output.
     * @param bool $personal True for the personal-key tier, false for the site-key tier.
     * @param int $userid User whose personal keys are read in the personal tier.
     * @param array $lasterror Updated in place with the last failing provider result.
     * @param array $attempts Appended to in place with one entry per provider called.
     * @return array|null A successful result, or null when no provider in this tier succeeded.
     */
    protected function try_key_tier(
        string $system,
        string $user,
        bool $jsonmode,
        bool $personal,
        int $userid,
        array &$lasterror,
        array &$attempts
    ): ?array {
        $keysource = $personal ? 'personal' : 'site';
        $key = function (string $provider) use ($personal, $userid): string {
            return $personal
                ? keys::get_personal_key($provider, $userid)
                : keys::get_site_key($provider);
        };

        $geminikey = $key(keys::PROVIDER_GEMINI);
        if ($geminikey !== '') {
            $result = $this->call_gemini($system, $user, $geminikey, $jsonmode);
            $attempts[] = $this->attempt($result, $keysource);
            if ($result['success']) {
                return $result + ['keysource' => $keysource];
            }
            $lasterror = $result;
        }

        $groqkey = $key(keys::PROVIDER_GROQ);
        if ($groqkey !== '') {
            $result = $this->call_groq($system, $user, $groqkey, $jsonmode);
            $attempts[] = $this->attempt($result, $keysource);
            if ($result['success']) {
                return $result + ['keysource' => $keysource];
            }
            $lasterror = $result;
        }

        $deepseekkey = $key(keys::PROVIDER_DEEPSEEK);
        if ($deepseekkey !== '') {
            $result = $this->call_deepseek($system, $user, $deepseekkey, $jsonmode);
            $attempts[] = $this->attempt($result, $keysource);
            if ($result['success']) {
                return $result + ['keysource' => $keysource];
            }
            $lasterror = $result;
        }

        $openaikey = $key(keys::PROVIDER_OPENAI);
        if ($openaikey !== '') {
            // URL and model follow the same tier as the key: the personal tier
            // prefers the user's own endpoint/model, falling back to the site value.
            if ($personal) {
                $rawurl = keys::get_personal_openai_url($userid);
                if ($rawurl === '') {
                    $rawurl = keys::get_openai_baseurl();
                }
                $model = keys::get_personal_openai_model($userid);
                if ($model === '') {
                    $model = keys::get_openai_model();
                }
            } else {
                $rawurl = keys::get_openai_baseurl();
                $model = keys::get_openai_model();
            }
            $openaiurl = $this->resolve_openai_url($rawurl);
            if ($this->is_safe_url($openaiurl)) {
                $result = $this->call_openai_compatible($system, $user, $openaikey, $openaiurl, $model, $jsonmode);
            } else {
                // A refused endpoint is a failure like any other: without a trace the usage
                // report would show nothing wrong while the configured provider is never used.
                $result = [
                    'success' => false,
                    'provider' => 'OpenAI',
                    'model' => $model,
                    'message' => 'OpenAI: ' . get_string('endpointblocked', 'local_aihub'),
                ];
            }
            $attempts[] = $this->attempt($result, $keysource);
            if ($result['success']) {
                return $result + ['keysource' => $keysource];
            }
            $lasterror = $result;
        }

        return null;
    }

    /**
     * Reduces a provider result to the fields the usage log stores.
     *
     * @param array $result The array a call_* method returned.
     * @param string $keysource Which key tier was used: personal or site.
     * @return array
     */
    protected function attempt(array $result, string $keysource): array {
        return [
            'provider' => (string) ($result['provider'] ?? ''),
            'model' => (string) ($result['model'] ?? ''),
            'keysource' => $keysource,
            'success' => !empty($result['success']),
            'message' => (string) ($result['message'] ?? ''),
        ];
    }

    /**
     * Calls the Gemini generative language API.
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param string $key Gemini API key.
     * @param bool $jsonmode Whether to force JSON output.
     * @return array HTTP result array.
     */
    protected function call_gemini(string $system, string $user, string $key, bool $jsonmode): array {
        $url = 'https://generativelanguage.googleapis.com/v1beta/models/'
            . 'gemini-flash-latest:generateContent';
        $data = ['contents' => [['parts' => [['text' => $user]]]]];
        if ($system !== '') {
            $data['systemInstruction'] = ['parts' => [['text' => $system]]];
        }
        if ($jsonmode) {
            $data['generationConfig'] = ['responseMimeType' => 'application/json'];
        }
        return $this->http_post(
            $url,
            json_encode($data),
            ['Content-Type: application/json', 'x-goog-api-key: ' . $key],
            'Gemini'
        ) + ['model' => 'gemini-flash-latest'];
    }

    /**
     * Calls the Groq inference API.
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param string $key Groq API key.
     * @param bool $jsonmode Whether to force JSON output.
     * @return array HTTP result array.
     */
    protected function call_groq(string $system, string $user, string $key, bool $jsonmode): array {
        $url = 'https://api.groq.com/openai/v1/chat/completions';
        $data = [
            'model' => 'openai/gpt-oss-120b',
            'messages' => $this->build_chat_messages($system, $user),
        ];
        if ($jsonmode) {
            $data['response_format'] = ['type' => 'json_object'];
        }
        return $this->http_post(
            $url,
            json_encode($data),
            ['Authorization: Bearer ' . $key, 'Content-Type: application/json'],
            'Groq'
        ) + ['model' => 'openai/gpt-oss-120b'];
    }

    /**
     * Calls the DeepSeek inference API.
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param string $key DeepSeek API key.
     * @param bool $jsonmode Whether to force JSON output.
     * @return array HTTP result array.
     */
    protected function call_deepseek(string $system, string $user, string $key, bool $jsonmode): array {
        $url = 'https://api.deepseek.com/chat/completions';
        $data = [
            'model' => 'deepseek-v4-flash',
            'messages' => $this->build_chat_messages($system, $user),
        ];
        if ($jsonmode) {
            $data['response_format'] = ['type' => 'json_object'];
        }
        return $this->http_post(
            $url,
            json_encode($data),
            ['Authorization: Bearer ' . $key, 'Content-Type: application/json'],
            'DeepSeek'
        ) + ['model' => 'deepseek-v4-flash'];
    }

    /**
     * Calls any OpenAI-compatible chat completions endpoint.
     *
     * @param string $system System instruction (may be empty).
     * @param string $user User prompt text.
     * @param string $key API key.
     * @param string $endpointurl Full URL to the chat completions endpoint.
     * @param string $model Model identifier (e.g. gpt-4o-mini).
     * @param bool $jsonmode Whether to force JSON output.
     * @return array HTTP result array.
     */
    protected function call_openai_compatible(
        string $system,
        string $user,
        string $key,
        string $endpointurl,
        string $model,
        bool $jsonmode
    ): array {
        $modelname = $model !== '' ? $model : 'gpt-4o-mini';
        $data = [
            'model' => $modelname,
            'messages' => $this->build_chat_messages($system, $user),
        ];
        if ($jsonmode) {
            $data['response_format'] = ['type' => 'json_object'];
        }
        return $this->http_post(
            $endpointurl,
            json_encode($data),
            ['Authorization: Bearer ' . $key, 'Content-Type: application/json'],
            'OpenAI'
        ) + ['model' => $modelname];
    }

    /**
     * Builds an OpenAI-style messages array with an optional system message.
     *
     * @param string $system System instruction (omitted when empty).
     * @param string $user User prompt text.
     * @return array The messages array.
     */
    protected function build_chat_messages(string $system, string $user): array {
        $messages = [];
        if ($system !== '') {
            $messages[] = ['role' => 'system', 'content' => $system];
        }
        $messages[] = ['role' => 'user', 'content' => $user];
        return $messages;
    }

    /**
     * Ensures the URL ends with /chat/completions.
     *
     * Users who supply only a base URL (e.g. https://api.openai.com/v1 or
     * https://openrouter.ai/api/v1) get the path appended automatically. URLs that
     * already include the full path are returned unchanged.
     *
     * @param string $url The configured endpoint URL.
     * @return string URL guaranteed to end with /chat/completions.
     */
    protected function resolve_openai_url(string $url): string {
        if (!str_ends_with($url, '/chat/completions')) {
            $url = rtrim($url, '/') . '/chat/completions';
        }
        return $url;
    }

    /**
     * Says why an OpenAI-compatible endpoint would be refused, or nothing when it would not.
     *
     * The same rule a call applies, so a value can be refused when it is saved instead of
     * being accepted and then ignored on every request.
     *
     * @param string $url The endpoint exactly as typed, before any cleaning. Empty means the
     *                    default endpoint.
     * @return string The reason, or an empty string when the endpoint is acceptable.
     */
    public function endpoint_problem(string $url): string {
        if ($url === '') {
            return '';
        }

        // PARAM_URL answers an address it cannot parse with an empty string, which a form
        // would then take for "forget the endpoint": it has to be refused while the typed
        // value is still in hand.
        if (clean_param($url, PARAM_URL) === '') {
            return get_string('endpointinvalid', 'local_aihub');
        }

        return $this->is_safe_url($this->resolve_openai_url($url)) ? '' : get_string('endpointblocked', 'local_aihub');
    }

    /**
     * Returns true when the URL is safe to use as an AI endpoint.
     *
     * Enforces HTTPS and blocks loopback, link-local, and RFC-1918 private
     * addresses to prevent SSRF via a configurable endpoint. Resolves A/AAAA DNS
     * records to block rebinding attacks where a public domain points to an
     * internal IP.
     *
     * @param string $url The URL to validate.
     * @return bool True if safe; false otherwise.
     */
    protected function is_safe_url(string $url): bool {
        $parsed = parse_url($url);
        if (!$parsed || ($parsed['scheme'] ?? '') !== 'https') {
            return false;
        }
        $host = $parsed['host'] ?? '';
        if (empty($host)) {
            return false;
        }
        if (in_array(strtolower($host), ['localhost', '127.0.0.1', '::1'], true)) {
            return false;
        }
        $ip = filter_var($host, FILTER_VALIDATE_IP);
        if ($ip !== false) {
            $ispublic = filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE);
            if ($ispublic === false) {
                return false;
            }
        } else {
            foreach ($this->resolve_dns($host) as $resolvedip) {
                $ispublic = filter_var(
                    $resolvedip,
                    FILTER_VALIDATE_IP,
                    FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE
                );
                if ($ispublic === false) {
                    return false;
                }
            }
        }
        return true;
    }

    /**
     * Resolves a hostname's A and AAAA records to a flat list of IP strings.
     *
     * Isolated from {@see self::is_safe_url()} so tests can stub DNS resolution
     * without depending on real network lookups.
     *
     * @param string $host The hostname to resolve.
     * @return string[] Resolved IPv4/IPv6 addresses (empty when none are found).
     */
    protected function resolve_dns(string $host): array {
        $resolvedips = [];
        $arecords = dns_get_record($host, DNS_A);
        if (is_array($arecords)) {
            foreach ($arecords as $r) {
                if (!empty($r['ip'])) {
                    $resolvedips[] = $r['ip'];
                }
            }
        }
        $aaaarecords = dns_get_record($host, DNS_AAAA);
        if (is_array($aaaarecords)) {
            foreach ($aaaarecords as $r) {
                if (!empty($r['ipv6'])) {
                    $resolvedips[] = $r['ipv6'];
                }
            }
        }
        return $resolvedips;
    }

    /**
     * Builds the HTTP client used for a provider call.
     *
     * The one seam in this class: every provider request goes through here, so a
     * test can answer with a canned response and exercise the parsing below, which
     * differs per provider and is otherwise only ever reached over the network.
     *
     * @return \curl
     */
    protected function make_curl(): \curl {
        global $CFG;
        require_once($CFG->libdir . '/filelib.php');

        return new \curl();
    }

    /**
     * Executes an HTTP POST using Moodle's curl wrapper and parses the response.
     *
     * @param string $url Target URL.
     * @param string $payload JSON-encoded POST body.
     * @param array $headers Array of header strings.
     * @param string $source Display name of the AI provider (for error messages).
     * @return array Keys: success (bool), data (string) on success, provider (string), message (string) on failure.
     */
    protected function http_post(string $url, string $payload, array $headers, string $source): array {
        $curl = $this->make_curl();
        $curl->setHeader($headers);
        $response = $curl->post($url, $payload, ['timeout' => self::HTTP_TIMEOUT]);
        $info = $curl->get_info();
        $code = isset($info['http_code']) ? (int) $info['http_code'] : 0;

        if ($curl->get_errno()) {
            return ['success' => false, 'message' => $source . ': ' . $curl->error, 'provider' => $source];
        }

        if ($code !== 200) {
            $decoded = json_decode($response, true);
            $extra = isset($decoded['error']['message'])
                ? $decoded['error']['message']
                : 'HTTP ' . $code;
            return ['success' => false, 'message' => $source . ': ' . $extra, 'provider' => $source];
        }

        $decoded = json_decode($response, true);
        $decoded = is_array($decoded) ? $decoded : [];
        $content = $source === 'Gemini'
            ? ($decoded['candidates'][0]['content']['parts'][0]['text'] ?? '')
            : ($decoded['choices'][0]['message']['content'] ?? '');

        // A 200 with nothing to read is not an answer: a blocked prompt, an interrupted
        // generation or a gateway's error page. Calling it a success would stop the chain
        // from trying the next provider and log a success that produced nothing.
        if (!is_string($content) || trim($content) === '') {
            $reason = $this->empty_response_reason($decoded, $source === 'Gemini');
            $text = $reason !== ''
                ? get_string('emptyresponsewhy', 'local_aihub', $reason)
                : get_string('emptyresponse', 'local_aihub');

            return ['success' => false, 'message' => $source . ': ' . $text, 'provider' => $source];
        }

        return ['success' => true, 'data' => $content, 'provider' => $source];
    }

    /**
     * Reads, from an answer with no text, the reason the provider gave for it.
     *
     * @param array $decoded The decoded response body.
     * @param bool $gemini Whether the body has Gemini's shape rather than the chat completions one.
     * @return string The reason, or an empty string when the provider gave none.
     */
    protected function empty_response_reason(array $decoded, bool $gemini): string {
        if ($gemini) {
            $reason = $decoded['promptFeedback']['blockReason'] ?? null;
            if ($reason !== null) {
                return 'blockReason: ' . $reason;
            }
            $reason = $decoded['candidates'][0]['finishReason'] ?? null;

            return $reason !== null ? 'finishReason: ' . $reason : '';
        }

        $reason = $decoded['choices'][0]['finish_reason'] ?? null;

        return $reason !== null ? 'finish_reason: ' . $reason : '';
    }
}
