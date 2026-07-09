const WALLET_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

const CLAIMS_OWNER = 'tiagopaidotheodoro-design';
const CLAIMS_REPO = 'rocketcapy-claims';
const CLAIMS_PATH = 'claims.jsonl';

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'Content-Type': 'application/json',
  };
}

async function appendClaim(token, entry) {
  const apiUrl = `https://api.github.com/repos/${CLAIMS_OWNER}/${CLAIMS_REPO}/contents/${CLAIMS_PATH}`;

  // até 3 tentativas: o sha muda se outra submissão gravar entre o GET e o PUT
  for (let attempt = 0; attempt < 3; attempt++) {
    let sha;
    let existingContent = '';

    const getRes = await fetch(apiUrl, { headers: ghHeaders(token) });
    if (getRes.status === 200) {
      const data = await getRes.json();
      sha = data.sha;
      existingContent = Buffer.from(data.content, 'base64').toString('utf-8');
    } else if (getRes.status !== 404) {
      throw new Error(`GitHub GET falhou: ${getRes.status}`);
    }

    const separator = existingContent && !existingContent.endsWith('\n') ? '\n' : '';
    const newContent = existingContent + separator + JSON.stringify(entry) + '\n';

    const putRes = await fetch(apiUrl, {
      method: 'PUT',
      headers: ghHeaders(token),
      body: JSON.stringify({
        message: `claim: ${entry.wallet.slice(0, 6)}…`,
        content: Buffer.from(newContent, 'utf-8').toString('base64'),
        ...(sha ? { sha } : {}),
      }),
    });

    if (putRes.ok) return;
    if (putRes.status !== 409) {
      throw new Error(`GitHub PUT falhou: ${putRes.status} ${await putRes.text()}`);
    }
    // 409 = conflito de sha, outra submissão venceu a corrida — tenta de novo
  }

  throw new Error('GitHub PUT falhou após 3 tentativas (conflito de sha)');
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method not allowed' });
  }

  const token = process.env.GITHUB_CONTENTS_TOKEN;
  if (!token) {
    console.error('GITHUB_CONTENTS_TOKEN não configurado');
    return res.status(500).json({ error: 'server misconfigured' });
  }

  const { wallet, xHandle } = req.body || {};

  if (typeof wallet !== 'string' || !WALLET_RE.test(wallet)) {
    return res.status(400).json({ error: 'invalid wallet address' });
  }
  if (typeof xHandle !== 'string' || !xHandle.trim()) {
    return res.status(400).json({ error: 'missing X handle' });
  }

  try {
    await appendClaim(token, {
      wallet,
      xHandle: xHandle.trim(),
      ts: new Date().toISOString(),
    });
    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('claim submission failed', err);
    return res.status(500).json({ error: 'internal error' });
  }
}
