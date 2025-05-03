# chachat/backend/services/embedding.py

import openai
import os
from dotenv import load_dotenv

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

# ここに関数を実装
def get_embedding(text: str, model="text-embedding-3-large"): # モデル名を修正
   text = text.replace("\n", " ") # Embeddingモデルの推奨事項
   try:
       # openai v1.x.x 以降の書き方 (推奨)
       client = openai.OpenAI() # クライアントを初期化
       response = client.embeddings.create(input=[text], model=model)
       return response.data[0].embedding
       # openai v0.x.x の書き方 (古いバージョンを使用している場合)
       # response = openai.Embedding.create(input=[text], model=model)
       # return response['data'][0]['embedding']
   except Exception as e:
       print(f"Error getting embedding: {e}")
       return None

# --- ChromaDB関連のコードは retriever.py に移動 ---
