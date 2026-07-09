import { put } from '@vercel/blob';

const WALLET_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method not allowed' });
  }

  const { wallet, xHandle } = req.body || {};

  if (typeof wallet !== 'string' || !WALLET_RE.test(wallet)) {
    return res.status(400).json({ error: 'invalid wallet address' });
  }
  if (typeof xHandle !== 'string' || !xHandle.trim()) {
    return res.status(400).json({ error: 'missing X handle' });
  }

  try {
    const entry = {
      wallet,
      xHandle: xHandle.trim(),
      ts: new Date().toISOString(),
    };
    // um arquivo por envio -> sem leitura/escrita concorrente pra resolver
    const key = `claims/${Date.now()}-${wallet}.json`;

    await put(key, JSON.stringify(entry), {
      access: 'private',
      contentType: 'application/json',
      addRandomSuffix: false,
    });

    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('claim submission failed', err);
    return res.status(500).json({ error: 'internal error' });
  }
}
