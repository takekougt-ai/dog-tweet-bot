import json
import os
import time
import requests
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google import genai
from google.genai import types
from requests_oauthlib import OAuth1

# --- 設定 ---
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
DRIVE_POSTED_X_FOLDER_ID = os.environ["DRIVE_POSTED_X_FOLDER_ID"]
DRIVE_POSTED_THREADS_FOLDER_ID = os.environ["DRIVE_POSTED_THREADS_FOLDER_ID"]
X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_TOKEN_SECRET = os.environ["X_ACCESS_TOKEN_SECRET"]

PROMPT = """この犬の写真を見て、投稿する文章を日本語で1つ書いてください。

以下のルールを厳守してください：
- 絵文字は一切使わない
- 文末に必ず以下のハッシュタグを全て付ける：#シーズー #ポメラニアン #ポメズー #犬
- 感嘆符（！）や過剰な句読点を避ける
- 「かわいい」「癒される」などの陳腐な表現は使わない
- おしゃれでシュールなトーンで書く
- 犬を擬人化したり、哲学的・文学的な視点で描写してもよい
- ハッシュタグを除いた本文は3行以内・100文字以内
- 文章のみ返答してください"""

def get_drive_client():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_credentials():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    creds.refresh(requests.Request())
    return creds

def download_next_photo(drive) -> tuple[str, str] | None:
    results = drive.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name)", pageSize=1, orderBy="createdTime"
    ).execute()

    files = results.get("files", [])
    if not files:
        print("投稿できる写真がありません")
        return None

    file = files[0]
    content = drive.files().get_media(fileId=file["id"]).execute()
    local_path = f"/tmp/{file['name']}"
    with open(local_path, "wb") as f:
        f.write(content)

    print(f"ダウンロード完了: {file['name']}")
    return local_path, file["id"]

def convert_to_jpeg(local_path: str) -> str:
    if not local_path.lower().endswith(".heic"):
        return local_path

    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()

    jpeg_path = local_path.rsplit(".", 1)[0] + ".jpg"
    img = Image.open(local_path)
    img.save(jpeg_path, "JPEG")
    print(f"HEIC→JPEG変換完了: {jpeg_path}")
    return jpeg_path

def upload_to_folder(local_path: str, folder_id: str, creds):
    """シンプルなmultipart uploadでDriveにアップロード"""
    file_name = os.path.basename(local_path)
    ext = file_name.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    metadata = json.dumps({"name": file_name, "parents": [folder_id]})
    with open(local_path, "rb") as f:
        image_data = f.read()

    res = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "metadata": ("metadata", metadata, "application/json"),
            "file": (file_name, image_data, mime_type)
        }
    )
    print(f"Driveアップロード: {res.status_code}")
    res.raise_for_status()
    return res.json()["id"]

def delete_if_both_posted(drive, file_id: str, local_path: str):
    file_name = os.path.basename(local_path)

    results = drive.files().list(
        q=f"'{DRIVE_POSTED_THREADS_FOLDER_ID}' in parents and name='{file_name}' and trashed=false",
        fields="files(id)"
    ).execute()

    if results.get("files"):
        drive.files().delete(fileId=file_id).execute()
        print("両方投稿済みのため元ファイルを削除しました")
    else:
        print("Threads側未投稿のため元ファイルは保持します")

def generate_caption(image_path: str) -> str:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    ext = image_path.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    with open(image_path, "rb") as f:
        image_data = f.read()

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=[types.Part.from_bytes(data=image_data, mime_type=mime_type), PROMPT]
    )
    return response.text.strip()

def upload_media(image_path: str) -> str:
    auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    ext = image_path.split(".")[-1].lower()
    mime_type = "image/png" if ext == "png" else "image/jpeg"

    with open(image_path, "rb") as f:
        image_data = f.read()

    init_res = requests.post(
        "https://upload.twitter.com/1.1/media/upload.json",
        auth=auth,
        data={"command": "INIT", "media_type": mime_type, "total_bytes": len(image_data)}
    )
    print(f"INIT: {init_res.status_code}")
    init_res.raise_for_status()
    media_id = init_res.json()["media_id_string"]

    append_res = requests.post(
        "https://upload.twitter.com/1.1/media/upload.json",
        auth=auth,
        data={"command": "APPEND", "media_id": media_id, "segment_index": 0},
        files={"media": image_data}
    )
    print(f"APPEND: {append_res.status_code}")
    append_res.raise_for_status()

    finalize_res = requests.post(
        "https://upload.twitter.com/1.1/media/upload.json",
        auth=auth,
        data={"command": "FINALIZE", "media_id": media_id}
    )
    print(f"FINALIZE: {finalize_res.status_code}")
    finalize_res.raise_for_status()
    print(f"メディアアップロード完了: {media_id}")
    return media_id

def post_to_x(media_id: str, caption: str):
    auth = OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    res = requests.post(
        "https://api.twitter.com/2/tweets",
        auth=auth,
        json={"text": caption, "media": {"media_ids": [media_id]}}
    )
    print(f"投稿レスポンス: {res.status_code} {res.text}")
    res.raise_for_status()
    print(f"X投稿完了！ ID: {res.json()['data']['id']}")

def update_log(file_name: str):
    log_path = "posted_log.json"
    with open(log_path) as f:
        log = json.load(f)
    log["posted"].append(file_name)
    log["history"].append({"file": file_name, "posted_at": datetime.utcnow().isoformat(), "platform": "x"})
    with open(log_path, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print("ログ更新完了")

def main():
    import google.auth.transport.requests

    drive = get_drive_client()
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )

    result = download_next_photo(drive)
    if not result:
        return

    local_path, file_id = result
    local_path = convert_to_jpeg(local_path)
    file_name = os.path.basename(local_path)

    caption = generate_caption(local_path)
    print(f"生成された文章:\n{caption}")

    media_id = upload_media(local_path)
    post_to_x(media_id, caption)

    upload_to_folder(local_path, DRIVE_POSTED_X_FOLDER_ID, creds)
    delete_if_both_posted(drive, file_id, local_path)
    update_log(file_name)

if __name__ == "__main__":
    main()
