"""The model that reads the evidence, chosen by the user:
- anthropic: Claude through the Anthropic API (pip install "foothold[claude]", ANTHROPIC_API_KEY);
- openai: any OpenAI-compatible API (OPENAI_API_KEY, FOOTHOLD_BASE_URL for another provider);
- local: LM Studio or Ollama on this machine, free and private (nothing but the page requests leaves it).
Settings come from the environment: FOOTHOLD_PROVIDER, FOOTHOLD_MODEL, FOOTHOLD_BASE_URL."""
import json
import os
import re
import urllib.error
import urllib.request

TIMEOUT = 900
LOCAL_URL = 'http://localhost:1234/v1'  # LM Studio; Ollama is http://localhost:11434/v1


class LLMError(Exception):
    pass


def settings():
    env = os.environ
    # An address given without a provider is an OpenAI-compatible server: an Anthropic key is never sent to it
    provider = env.get('FOOTHOLD_PROVIDER') or (
        'anthropic' if env.get('ANTHROPIC_API_KEY') and not env.get('FOOTHOLD_BASE_URL') else
        'openai' if env.get('OPENAI_API_KEY') else 'local')
    if provider not in ('anthropic', 'openai', 'local'):
        raise LLMError(f'unknown provider "{provider}": use anthropic, openai or local')
    return {'provider': provider, 'model': env.get('FOOTHOLD_MODEL', ''), 'base_url': env.get('FOOTHOLD_BASE_URL', '')}


def _http(url, body=None, key=None):
    headers = {'Content-Type': 'application/json'} | ({'Authorization': f'Bearer {key}'} if key else {})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # the user's own model server, local ones included
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise LLMError(f'{url}: HTTP {e.code} {e.read().decode("utf-8", "replace")[-300:]}')
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError(f'{url}: {getattr(e, "reason", e)}')


def _openai_compatible(system, prompt, cfg, key):
    base = (cfg['base_url'] or (LOCAL_URL if cfg['provider'] == 'local' else 'https://api.openai.com/v1')).rstrip('/')
    if not base.startswith(('http://', 'https://')):
        raise LLMError('FOOTHOLD_BASE_URL must start with http:// or https://')
    model = cfg['model']
    if not model and cfg['provider'] == 'local':
        loaded = _http(base + '/models').get('data') or []
        model = next((m['id'] for m in loaded if 'embed' not in m['id']), '')
    if not model:
        raise LLMError('no model set: FOOTHOLD_MODEL or --model')
    cfg['model'] = model
    out = _http(base + '/chat/completions', {'model': model, 'messages': [
        {'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}, key)
    try:
        text = out['choices'][0]['message']['content'] or ''
    except (KeyError, IndexError, TypeError):
        raise LLMError(f'unexpected answer from {base}: {str(out)[:200]}')
    return re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()  # reasoning models think out loud first


def _anthropic(system, prompt, cfg):
    try:
        import anthropic
    except ImportError:
        raise LLMError('the Anthropic SDK is not installed: pip install "foothold[claude]"')
    cfg['model'] = cfg['model'] or 'claude-opus-5-5'
    client = anthropic.Anthropic(base_url=cfg['base_url'] or None, timeout=TIMEOUT)
    try:
        # Streaming for a long input; a declined request falls back to another model on the server side
        with client.beta.messages.stream(model=cfg['model'], max_tokens=32000, system=system,
                                         betas=['server-side-fallback-2026-07-01'], fallbacks='default',
                                         thinking={'type': 'adaptive'}, output_config={'effort': 'high'},
                                         messages=[{'role': 'user', 'content': prompt}]) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError('the Anthropic API key was refused (ANTHROPIC_API_KEY)')
    except anthropic.RateLimitError as e:
        raise LLMError(f'Anthropic API rate limit: {e.message}')
    except anthropic.APIStatusError as e:
        raise LLMError(f'Anthropic API error {e.status_code}: {e.message}')
    except anthropic.APIConnectionError:
        raise LLMError('cannot reach the Anthropic API')
    cfg['model'] = message.model  # the model that answered, which a fallback may have changed
    if message.stop_reason == 'refusal':
        raise LLMError('the model declined the request')
    if message.stop_reason == 'max_tokens':
        raise LLMError('the answer was cut off before the end')
    return ''.join(block.text for block in message.content if block.type == 'text')


def ask(system, prompt, cfg):
    """The model's answer as text. Fills cfg['model'] with the model that answered."""
    if cfg['provider'] == 'anthropic':
        return _anthropic(system, prompt, cfg)
    key = os.environ.get('OPENAI_API_KEY') if cfg['provider'] == 'openai' else None
    if cfg['provider'] == 'openai' and not key:
        raise LLMError('no API key: set OPENAI_API_KEY')
    return _openai_compatible(system, prompt, cfg, key)


def extract_json(text):
    """The JSON object in an answer, even wrapped in a code fence or a sentence."""
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise LLMError('no JSON object in the model answer')
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise LLMError(f'the model answer is not valid JSON: {e}')
