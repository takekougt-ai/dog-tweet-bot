import json
import os
import time
import subprocess
import shutil
import requests
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google import genai
from google.genai import types

# --- 設定 ---
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_THREADS_FOLDER_ID = os.environ["DRIVE_POSTED_THREADS_FOLDER_ID"]
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]

PROMPT = """この犬の写真を見て、投稿する文章を日本語で1つ書いてください。

以下のルールを厳守してください：
- 絵文字は一切使わない
- ハッシュタグは「シーズー」「ポメラニアン」「ポメズー」「犬」
- 感嘆符（！）や過剰な句読点を避ける
- 「かわいい」「癒される」などの陳腐な表現は使わない
- おしゃれでシュールなトーンで書く
- 犬を擬人化したり、哲学的・文学的な視点で描写してもよい
- 短くて余白のある文章が望ましい（3行以内）
- 140文字以内
- 文章のみ返答してください"""

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

# --- posted_threadsフォルダにコピー ---
def copy_to_posted_threads(drive, file_id: str):
    drive.files().copy(
        fileId=file_id,
        body={"parents": [DRIVE_POSTED_THREADS_FOLDER_ID]}
    ).execute()
    print("posted_threadsフォルダにコピーしました")

# --- 両方投稿済みなら元フォルダから削除 ---
def delete_if_both_posted(drive, file_id: str):
    posted_x_folder_id = os.environ["DRIVE_POSTED_X_FOLDER_ID"]
    file_info = drive.files().get(fileId=file_id, fields="name").execute()
    file_name = file_info["name"]

    results = drive.files().list(
        q=f"'{posted_x_folder_id}' in parents and name='{file_name}' and trashed=false",
        fields="files(id)"
    ).execute()

    if results.get("files"):
        drive.files().delete(fileId=file_id).execute()
        print("両方投稿済みのため元ファイルを削除しました")
    else:
        print("X側未投稿のため元ファイルは保持します")

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
        model="gemini-2.5-flash-lite",
        contents=[
            types.Part.from_bytes(data=image_data, mime_type=mime_type),
            PROMPT
        ]
    )
    return response.text.strip()

# --- Threadsに画像付きで投稿 ---
def post_to_threads(image_url: str, caption: str):
    token = THREADS_ACCESS_TOKEN

    me_res = requests.get(
        "https://graph.threads.net/v1.0/me",
        params={"fields": "id", "access_token": token}
    )
    me_res.raise_for_status()
    user_id = me_res.json()["id"]
    print(f"ユーザーID: {user_id}")

    container_res = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads",
        params={"access_token": token},
        json={"media_type": "IMAGE", "image_url": image_url, "text": caption}
    )
    print(f"コンテナレスポンス: {container_res.status_code} {container_res.text}")
    container_res.raise_for_status()
    container_id = container_res.json()["id"]

    time.sleep(10)

    publish_res = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads_publish",
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
    log["history"].append({"file": file_name, "posted_at": datetime.utcnow().isoformat(), "platform": "threads"})
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

    image_url = push_image_and_get_url(local_path)
    caption = generate_caption(local_path)
    print(f"生成された文章:\n{caption}")

    post_to_threads(image_url, caption)
    copy_to_posted_threads(drive, file_id)
    delete_if_both_posted(drive, file_id)
    update_log(file_name)

    subprocess.run(["git", "add", "posted_log.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Update posted log [threads]"], check=False)
    subprocess.run(["git", "push"], check=True)

if __name__ == "__main__":
    main()
