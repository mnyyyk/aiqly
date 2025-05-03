# backend/services/chat.py (会話履歴対応 + モデル名 gpt-4.1 指定版)

import openai
import os
from dotenv import load_dotenv
import traceback

# ▼▼▼ DBとUserモデル、デフォルトプロンプトをインポート ▼▼▼
from backend.extensions import db
from backend.models import User, DEFAULT_PROMPT_ROLE, DEFAULT_PROMPT_TASK  # デフォルト値も使う可能性があるのでインポート
# ▲▲▲ DBとUserモデル、デフォルトプロンプトをインポート ▲▲▲

# retriever から関数を直接インポート
from backend.services.retriever import retrieve_similar_docs

load_dotenv()

# OpenAIクライアント初期化 (変更なし)
client = None
try:
    # 環境変数からAPIキーを読み込むことを確認
    if not os.getenv("OPENAI_API_KEY"):
        print("CRITICAL ERROR: OPENAI_API_KEY environment variable not set.")
    else:
        client = openai.OpenAI(); print("OpenAI client object created.")
except AttributeError: print("CRITICAL ERROR: openai.OpenAI() not found. Ensure 'openai' library is installed and updated (pip install --upgrade openai)."); traceback.print_exc()
except Exception as init_error: print(f"CRITICAL: Failed to init OpenAI client: {init_error}"); traceback.print_exc()


# --- ▼▼▼ answer_question 関数 (history引数を追加、messages構築を変更、モデル名指定) ▼▼▼ ---
def answer_question(question: str, user_id: int, history: list[dict] = []) -> str:
    """
    質問応答の中核機能。会話履歴と指定されたユーザーのDB設定、知識を使って回答を生成。
    Args:
        question (str): ユーザーからの現在の質問。
        user_id (int): ユーザーID。
        history (list[dict]): 会話履歴。各要素は {"role": "user" or "assistant", "content": ...} の形式。
    Returns:
        str: AIからの回答。
    """
    global client
    if client is None:
        print("Error in answer_question: OpenAI client is not initialized (None). Check initialization logs.")
        return "申し訳ありません、AIモデルへの接続設定に問題があります。"
    if user_id is None:
        print("Error in answer_question: user_id is required but was None.")
        return "エラー：ユーザー情報が特定できません。"
    if not isinstance(history, list): # history の型チェックを追加
        print(f"Warning in answer_question: received invalid history type: {type(history)}. Resetting to empty list for user {user_id}.")
        history = []

    print(f"\n--- [DEBUG Chat Service] Answering question for user {user_id} ---")
    print(f"--- [DEBUG Chat Service] Question: '{question}'")
    print(f"--- [DEBUG Chat Service] Received History Length: {len(history)}")
    # 詳細な履歴内容ログ (必要な場合のみコメント解除)
    # if history:
    #     print(f"--- [DEBUG Chat Service] Received History Content (last 2 items): {history[-2:]}")

    # --- データベースからユーザー設定（プロンプト含む）を取得 ---
    user = None
    try:
        user = db.session.get(User, user_id)
        if user is None:
             print(f"Warning in answer_question: User with id {user_id} not found in DB. Using default prompts.")
    except Exception as db_error:
        print(f"Error fetching user {user_id} from DB in answer_question: {db_error}")
        traceback.print_exc()
        # DBエラーの場合もデフォルトプロンプトを使用

    # プロンプトを取得 (ユーザーが存在しない or 未設定ならデフォルト値)
    role_prompt = user.current_prompt_role if user else DEFAULT_PROMPT_ROLE
    task_prompt = user.current_prompt_task if user else DEFAULT_PROMPT_TASK
    print(f"--- [DEBUG Chat Service] Using Role Prompt: '{role_prompt[:50]}...'")
    print(f"--- [DEBUG Chat Service] Using Task Prompt: '{task_prompt[:50]}...'")

    # --- 1. 類似ドキュメントを取得 ---
    results = None
    context = "" # コンテキストは必ず初期化しておく
    try:
        print(f"--- [DEBUG Chat Service] Retrieving similar documents for user {user_id}...")
        results = retrieve_similar_docs(question, user_id, top_k=3) # retriever.py 内の関数を呼び出し
        if results and results.get('documents') and results['documents'][0]:
            context_texts = results['documents'][0]
            context = "\n\n---\n\n".join(context_texts) # コンテキスト文字列を作成
            print(f"--- [DEBUG Chat Service] Found {len(context_texts)} relevant chunks.")
        else:
            print(f"--- [DEBUG Chat Service] No relevant chunks found.")
    except Exception as retrieve_error:
        print(f"Error during retrieval for user {user_id} in answer_question: {retrieve_error}");
        traceback.print_exc()
        # 検索エラーが発生しても処理は続行するが、エラーメッセージを返す
        return "関連情報の検索中にエラーが発生しました。"

    print(f"--- [DEBUG Chat Service] Context length (chars): {len(context)}")
    # コンテキスト内容のログ (必要な場合のみコメント解除)
    # if context:
    #     print(f"--- [DEBUG Chat Service] Context Content Snippet:\n{context[:200]}...")

    # --- 3. OpenAI APIに渡すメッセージリストの構築 ---
    # システムプロンプト (Role, Task, Context を結合)
    system_message_content = f"{role_prompt}\n\n{task_prompt}"
    if context: # コンテキストがある場合のみ追加
        system_message_content += f"\n\n【内部文書（コンテキスト）】\n{context}"
    system_message = {"role": "system", "content": system_message_content}

    # 会話履歴を整形 (不正な形式はスキップ)
    history_messages = []
    invalid_history_items = 0
    for message in history:
        if isinstance(message, dict) and "role" in message and "content" in message \
           and message["role"] in ["user", "assistant"] and isinstance(message["content"], str):
            history_messages.append({"role": message["role"], "content": message["content"]})
        else:
            invalid_history_items += 1
            print(f"--- [DEBUG Chat Service] Skipping invalid history item for user {user_id}: {message}")
    if invalid_history_items > 0:
        print(f"--- [DEBUG Chat Service] Skipped {invalid_history_items} invalid history items for user {user_id}.")

    # 最新のユーザー質問
    # コンテキストはシステムメッセージに含まれているので、質問のみでOK
    user_message = {"role": "user", "content": question}

    # メッセージリストを結合 [システム, 過去の会話..., 最新の質問] の順序
    messages = [system_message] + history_messages + [user_message]

    print(f"--- [DEBUG Chat Service] Total messages constructed for OpenAI API: {len(messages)}")
    # メッセージ内容のログ (必要ならコメント解除)
    # print("--- [DEBUG Chat Service] Messages for OpenAI API (Snippets):")
    # for i, msg in enumerate(messages):
    #     print(f"  [{i}] Role: {msg['role']}, Content: {msg['content'][:70]}...")


    # --- 4. OpenAI API 呼び出し ---
    try:
        print(f"--- [DEBUG Chat Service] Sending request to OpenAI API (Model: gpt-4.1)...")
        response = client.chat.completions.create(
            model="gpt-4.1", # ★★★ モデル名を指定 ★★★
            messages=messages,
            temperature=0.3, # 応答の多様性を少し出す
            max_tokens=1500  # 回答の最大トークン数
            # stream=False # ストリーミングは今回は使用しない
        )
        answer = response.choices[0].message.content.strip()
        finish_reason = response.choices[0].finish_reason
        usage = response.usage # トークン使用量

        print(f"--- [DEBUG Chat Service] Received answer from OpenAI (Finish reason: {finish_reason})")
        if usage:
            print(f"--- [DEBUG Chat Service] OpenAI API Token Usage: Prompt={usage.prompt_tokens}, Completion={usage.completion_tokens}, Total={usage.total_tokens}")
        else:
            print("--- [DEBUG Chat Service] OpenAI API Token Usage information not available in response.")

        return answer

    # --- エラーハンドリング ---
    except openai.AuthenticationError as e:
        error_msg = f"OpenAI Authentication Error: {e}. Check your API key."
        print(error_msg); traceback.print_exc();
        return "AI認証エラーが発生しました。管理者にお問い合わせください。(APIキー設定を確認してください)"
    except openai.RateLimitError as e:
        error_msg = f"OpenAI Rate Limit Error: {e}. Please wait and try again later."
        print(error_msg); traceback.print_exc();
        return "AIへのリクエストが制限を超えました。しばらくしてから再度お試しください。"
    except openai.NotFoundError as e:
        error_msg = f"OpenAI Not Found Error (Model 'gpt-4.1' might be unavailable or misspelled): {e}"
        print(error_msg); traceback.print_exc();
        return "AIモデルが見つかりませんでした。管理者にお問い合わせください。"
    except openai.APIConnectionError as e:
        error_msg = f"OpenAI API Connection Error: {e}. Check network connectivity."
        print(error_msg); traceback.print_exc();
        return "AIサービスへの接続に失敗しました。ネットワーク接続を確認してください。"
    except openai.APIStatusError as e: # APIからのステータスエラー (例: 5xx)
        error_msg = f"OpenAI API Status Error: {e.status_code} - {e.message}"
        print(error_msg); traceback.print_exc();
        return f"AIサービスでエラーが発生しました (コード: {e.status_code})。しばらくしてから再度お試しください。"
    except Exception as e:
        error_msg = f"An unexpected error occurred during OpenAI API call: {e}"
        print(error_msg); traceback.print_exc();
        return "AIとの通信中に予期せぬエラーが発生しました。"
# --- ▲▲▲ answer_question 関数を修正 ▲▲▲