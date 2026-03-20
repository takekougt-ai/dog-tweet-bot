import json
import os
import time
import requests
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google import genai
from google.genai import types

# --- 設定 ---
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_FOLDER_ID = os.environ["DRIVE_POSTED_FOLDER_ID"]
THREADS_APP_ID = os.environ["THREADS_APP_ID"]
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]

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

# --- Google Driveで画像を公開URLとして共有 ---
def get_public_image_url(drive, file_id: str) -> str:
    # 一時的に公開設定にする
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/uc?export=download&id={file_id}"

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
            """この犬の写真を見て、Threadsでバズりやすい投稿文章を日本語で1つ作成してください。
条件：
- 200文字以内
- 絵文字を適度に使う
- 犬好きの心をつかむ表現
- ハッシュタグを2〜3個（#犬 #いぬのいる生活 など）
- 文章のみ返答してください"""
        ]
    )
    return response.text.strip()

# --- Threadsに画像付きで投稿 ---
def post_to_threads(image_url: str, caption: str):
    user_id = THREADS_APP_ID
    token = THREADS_ACCESS_TOKEN

    # Step1: メディアコンテナを作成
    container_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    container_res = requests.post(container_url, data={
        "media_type": "IMAGE",
        "image_url": image_url,
        "text": caption,
        "access_token": token
    })
    container_res.raise_for_status()
    container_id = container_res.json()["id"]
    print(f"コンテナ作成完了: {container_id}")

    # Step2: 処理待ち
    time.sleep(5)

    # Step3: 投稿を公開
    publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
    publish_res = requests.post(publish_url, data={
        "creation_id": container_id,
        "access_token": token
    })
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

    # 画像を公開URLとして取得
    image_url = get_public_image_url(drive, file_id)
    print(f"画像URL: {image_url}")

    # 文章生成
    caption = generate_caption(local_path)
    print(f"生成された文章:\n{caption}")

    # Threadsに投稿
    post_to_threads(image_url, caption)

    # 後処理
    move_to_posted(drive, file_id)
    update_log(file_name)

if __name__ == "__main__":
    main()
