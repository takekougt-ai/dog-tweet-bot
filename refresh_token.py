import os
import base64
import requests
from nacl.public import PublicKey, SealedBox

THREADS_APP_SECRET = os.environ["THREADS_APP_SECRET"]
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]

# --- 長期トークンに更新 ---
def refresh_threads_token() -> str:
    res = requests.get(
        "https://graph.threads.net/refresh_access_token",
        params={
            "grant_type": "th_refresh_token",
            "access_token": THREADS_ACCESS_TOKEN
        }
    )
    res.raise_for_status()
    new_token = res.json()["access_token"]
    print("Threadsトークン更新完了")
    return new_token

# --- GitHub SecretsをAPI経由で更新 ---
def update_github_secret(secret_name: str, secret_value: str):
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # リポジトリの公開鍵を取得
    pk_res = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
        headers=headers
    )
    pk_res.raise_for_status()
    pk_data = pk_res.json()
    public_key = pk_data["key"]
    key_id = pk_data["key_id"]

    # 公開鍵で暗号化
    pk_bytes = base64.b64decode(public_key)
    sealed_box = SealedBox(PublicKey(pk_bytes))
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    encrypted_value = base64.b64encode(encrypted).decode("utf-8")

    # Secretを更新
    update_res = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/{secret_name}",
        headers=headers,
        json={
            "encrypted_value": encrypted_value,
            "key_id": key_id
        }
    )
    update_res.raise_for_status()
    print(f"GitHub Secret '{secret_name}' 更新完了")

def main():
    new_token = refresh_threads_token()
    update_github_secret("THREADS_ACCESS_TOKEN", new_token)
    print("全ての更新が完了しました")

if __name__ == "__main__":
    main()
