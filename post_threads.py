import json
import os
import time
import shutil
import subprocess
import requests
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google import genai
from google.genai import types

# --- 設定 ---
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_FOLDER_ID = os.environ["DRIVE_POSTED_FOLDER_ID"]
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]  # 例: username/dog-tweet-bot

# --- Google Drive クライアント ---
def get_drive_client():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

# --- Google Driveから写真を1枚取得 ---
def download_next_photo(drive) -> tuple[str, str] | None:
    results = drive.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name)",
        pageSize=1,
        orderBy="createdTime"
    ).execute()

    files = results.get("files", [])
    if not files:
        print("投稿できる写真がありません")
        return None

    file = files[0]
    file_id = file["id"]
    file_name = file["name"]

    content = drive.files().get_media(fileId=file_id).execute()
    local_path = f"/tmp/{file_name}"
    with open(local_path, "wb") as f:
        f.write(content)

    print(f"ダウンロード完了: {file_name}")
    return local_path, file_id

# --- 投稿済みフォルダに移動 ---
def move_to_posted(drive, file_id: str):
    drive.files().update(
        fileId=file_id,
        addParents=DRIVE_POSTED_FOLDER_ID,
        removeParents=DRIVE_FOLDER_ID
    ).execute()
    print("投稿済みフォルダに移動しました")

# --- GitHubにpushしてraw URLを取得 ---
def push_image_and_get_url(local_path: str) -> str:
    file_name = os.path.basename(local_path)
    dest_path = f"posted_images/{file_name}"

    os.makedirs("posted_images", exist_ok=True)
    shutil.copy(local_path, dest_path)

    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
    subprocess.run(["git", "add", dest_path], check=True)
    subprocess.run(["git", "commit", "-m", f"Add image for posting: {file_name}"], check=False)
    subprocess.run(["git", "push"], check=True)

    raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{dest_path}"
    print(f"GitHub raw URL: {raw_url}")

    # GitHubにファイルが反映されるまで待つ
    time.sleep(5)
    return raw_url

# --- Geminiで文章生成 ---
def generate_caption(image_path: str) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    ext = image_path.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    with open(image_path, "rb") as f:
        image_data = f.read()

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite-preview-06-17",
        contents=[
            types.Part.from_bytes(data=image_data, mime_type=mime_type),
            """この犬の写真を見て、Threadsに投稿する文章を日本語で1つ書いてください。

以下のルールを厳守してください：
- 絵文字は一切使わない
- ハッシュタグは一切使わない
- 感嘆符（！）や過剰な句読点を避ける
- 「かわいい」「癒される」などの陳腐な表現は使わない
- おしゃれでシュールなトーンで書く
- 犬を擬人化したり、哲学的・文学的な視点で描写してもよい
- 短くて余白のある文章が望ましい（3行以内）
- 思わず「いいね」や「保存」したくなるような、じわじわくる面白さや共感を狙う
- 文章のみ返答してください"""
        ]
    )
    return response.text.strip()

# --- Threadsに画像付きで投稿 ---
def post_to_threads(image_url: str, caption: str):
    token = THREADS_ACCESS_TOKEN

    # Step0: ユーザーIDを取得
    me_res = requests.get(
        "https://graph.threads.net/v1.0/me",
        params={"fields": "id", "access_token": token}
    )
    me_res.raise_for_status()
    user_id = me_res.json()["id"]
    print(f"ユーザーID: {user_id}")

    # Step1: メディアコンテナを作成
    container_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    container_res = requests.post(
        container_url,
        params={"access_token": token},
        json={
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": caption
        }
    )
    print(f"コンテナレスポンス: {container_res.status_code} {container_res.text}")
    container_res.raise_for_status()
    container_id = container_res.json()["id"]
    print(f"コンテナ作成完了: {container_id}")

    # Step2: 処理待ち
    time.sleep(10)

    # Step3: 投稿を公開
    publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
    publish_res = requests.post(
        publish_url,
        params={"access_token": token},
        json={"creation_id": container_id}
    )
    print(f"公開レスポンス: {publish_res.status_code} {publish_res.text}")
    publish_res.raise_for_status()
    print(f"Threads投稿完了！ ID: {publish_res.json()['id']}")

# --- ログ更新 ---
def update_log(file_name: str):
    log_path = "posted_log.json"
    with open(log_path) as f:
        log = json.load(f)

    log["posted"].append(file_name)
    log["history"].append({
        "file": file_name,
        "posted_at": datetime.utcnow().isoformat()
    })

    with open(log_path, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print("ログ更新完了")

# --- メイン ---
def main():
    drive = get_drive_client()

    result = download_next_photo(drive)
    if not result:
        return

    local_path, file_id = result
    file_name = os.path.basename(local_path)

    # GitHubにpushしてraw URLを取得
    image_url = push_image_and_get_url(local_path)

    # 文章生成
    caption = generate_caption(local_path)
    print(f"生成された文章:\n{caption}")

    # Threadsに投稿
    post_to_threads(image_url, caption)

    # 後処理
    move_to_posted(drive, file_id)
    update_log(file_name)

    # ログもcommit
    subprocess.run(["git", "add", "posted_log.json"], check=True)
    subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
    subprocess.run(["git", "commit", "-m", "Update posted log"], check=False)
    subprocess.run(["git", "push"], check=True)

if __name__ == "__main__":
    main()
