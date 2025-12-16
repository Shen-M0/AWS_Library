import json
import boto3
from boto3.dynamodb.conditions import Key, Attr
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
table_books = dynamodb.Table('LibraryBooks')
table_users = dynamodb.Table('LibraryUsers')

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        if isinstance(obj, set):
            return list(obj)
        return super(DecimalEncoder, self).default(obj)

def build_response(code, body):
    return {
        'statusCode': code,
        'headers': {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Allow-Methods': 'OPTIONS,POST,GET,PUT,DELETE'
        },
        'body': json.dumps(body, cls=DecimalEncoder, ensure_ascii=False)
    }

def lambda_handler(event, context):
    print("Event:", event)
    
    if event['httpMethod'] == 'OPTIONS':
        return build_response(200, '')

    path = event['path']
    method = event['httpMethod']
    body = json.loads(event['body']) if event.get('body') else {}
    params = event.get('queryStringParameters') or {}

    try:
        # 1. 搜尋書籍 (GET /books)
        if path == '/books' and method == 'GET':
            category = params.get('category')
            keyword = params.get('q', '').lower()
            scan_kwargs = {}
            if category and category != 'All':
                scan_kwargs['FilterExpression'] = Attr('Category').eq(category)
            response = table_books.scan(**scan_kwargs)
            items = response.get('Items', [])
            if keyword:
                items = [i for i in items if keyword in i.get('Title','').lower() or keyword in i.get('Author','').lower()]
            return build_response(200, items)

        # 2. 書籍詳情 (GET /books/{isbn})
        elif path.startswith('/books/') and method == 'GET' and not path.endswith('/borrow') and not path.endswith('/return'):
            isbn = path.split('/')[-1]
            resp = table_books.get_item(Key={'ISBN': isbn})
            return build_response(200, resp.get('Item')) if 'Item' in resp else build_response(404, {'message': 'Not found'})

        # 3. 新增書籍 (POST /admin/books)
        elif path == '/admin/books' and method == 'POST':
            if not body.get('ISBN') or not body.get('Title'):
                return build_response(400, {'message': '缺少 ISBN 或 Title'})
            item = body
            # 確保數字欄位
            item['TotalCopies'] = int(item.get('TotalCopies', 1))
            item['AvailableCopies'] = int(item.get('TotalCopies', 1))
            item['BorrowCount'] = 0
            item['Borrowers'] = []
            item['Status'] = 'Available'
            clean_item = {k: v for k, v in item.items() if v != ""}
            table_books.put_item(Item=clean_item)
            return build_response(201, {'message': '新增成功'})

        # 4. 修改書籍 (PUT /admin/books/{isbn}) [邏輯強化版]
        elif path.startswith('/admin/books/') and method == 'PUT':
            isbn = path.split('/')[-1]
            
            # 先讀取舊資料，為了計算目前借出幾本
            old_data_resp = table_books.get_item(Key={'ISBN': isbn})
            if 'Item' not in old_data_resp:
                return build_response(404, {'message': '書籍不存在'})
            old_book = old_data_resp['Item']

            # 計算目前借出數量 = 舊總數 - 舊可用數
            current_borrowed = int(old_book.get('TotalCopies', 0)) - int(old_book.get('AvailableCopies', 0))
            if current_borrowed < 0: current_borrowed = 0 # 防呆

            # 取得新的總數
            new_total = int(body.get('TotalCopies', old_book.get('TotalCopies')))
            
            # --- 關鍵檢查：新總數不能小於已借出數量 ---
            if new_total < current_borrowed:
                return build_response(400, {
                    'message': f'修改失敗：目前已借出 {current_borrowed} 本，總館藏數量不能低於此數值。'
                })

            # 計算新的可用數量 = 新總數 - 目前借出
            new_available = new_total - current_borrowed

            item = body
            item['ISBN'] = isbn # 確保 Key 不變
            item['TotalCopies'] = new_total
            item['AvailableCopies'] = new_available
            
            # 保留系統欄位，不可由前端覆蓋
            item['BorrowCount'] = old_book.get('BorrowCount', 0)
            item['Borrowers'] = old_book.get('Borrowers', [])
            item['Status'] = 'Available' if new_available > 0 else 'OutOfStock'
            
            clean_item = {k: v for k, v in item.items() if v != ""}
            table_books.put_item(Item=clean_item)
            return build_response(200, {'message': '修改成功'})

        # 5. 刪除書籍 (DELETE /admin/books/{isbn})
        elif path.startswith('/admin/books/') and method == 'DELETE':
            isbn = path.split('/')[-1]
            # 刪除前檢查：如果有人借閱中，是否允許刪除？通常不允許，這裡加一個簡單檢查
            book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if book:
                current_borrowed = int(book.get('TotalCopies', 0)) - int(book.get('AvailableCopies', 0))
                if current_borrowed > 0:
                    return build_response(400, {'message': f'無法刪除：尚有 {current_borrowed} 本書未歸還。'})

            table_books.delete_item(Key={'ISBN': isbn})
            return build_response(200, {'message': '刪除成功'})

        # 6. 註冊 (POST /register)
        elif path == '/register' and method == 'POST':
            user_id = body.get('UserID')
            if not user_id: return build_response(400, {'message': '缺帳號'})
            if 'Item' in table_users.get_item(Key={'UserID': user_id}):
                return build_response(400, {'message': '帳號已存在'})
            new_user = {'UserID': user_id, 'Password': body.get('Password'), 'Name': body.get('Name'), 'Role': 'Member', 'BorrowedBooks': []}
            table_users.put_item(Item=new_user)
            return build_response(201, {'message': '註冊成功'})

        # 7. 借閱書籍 (POST /books/{isbn}/borrow)
        elif path.startswith('/books/') and path.endswith('/borrow') and method == 'POST':
            isbn = path.split('/')[2]
            user_id = body.get('UserID')
            if not user_id: return build_response(400, {'message': '未登入'})
            book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if not book or book['AvailableCopies'] <= 0: return build_response(400, {'message': '無法借閱'})
            user = table_users.get_item(Key={'UserID': user_id}).get('Item')
            if isbn in user.get('BorrowedBooks', []): return build_response(400, {'message': '已借閱過'})

            table_books.update_item(Key={'ISBN': isbn}, UpdateExpression="set AvailableCopies = AvailableCopies - :val, BorrowCount = BorrowCount + :val, Borrowers = list_append(if_not_exists(Borrowers, :empty), :uid)", ExpressionAttributeValues={':val': 1, ':uid': [user_id], ':empty': []})
            table_users.update_item(Key={'UserID': user_id}, UpdateExpression="set BorrowedBooks = list_append(if_not_exists(BorrowedBooks, :empty), :bid)", ExpressionAttributeValues={':bid': [isbn], ':empty': []})
            return build_response(200, {'message': '借閱成功'})

        # 8. 還書 (POST /books/{isbn}/return)
        elif path.startswith('/books/') and path.endswith('/return') and method == 'POST':
            isbn = path.split('/')[2]
            user_id = body.get('UserID')
            user = table_users.get_item(Key={'UserID': user_id}).get('Item')
            if not user or isbn not in user.get('BorrowedBooks', []): return build_response(400, {'message': '未借閱此書'})

            # 更新 User
            new_books = [b for b in user['BorrowedBooks'] if b != isbn]
            table_users.update_item(Key={'UserID': user_id}, UpdateExpression="set BorrowedBooks = :nb", ExpressionAttributeValues={':nb': new_books})
            
            # 更新 Book
            book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if book:
                new_borrowers = [u for u in book.get('Borrowers', []) if u != user_id]
                table_books.update_item(Key={'ISBN': isbn}, UpdateExpression="set AvailableCopies = AvailableCopies + :val, Borrowers = :nbs", ExpressionAttributeValues={':val': 1, ':nbs': new_borrowers})
            return build_response(200, {'message': '還書成功'})

        # 9. 登入
        elif path == '/login' and method == 'POST':
            user = table_users.get_item(Key={'UserID': body.get('UserID')}).get('Item')
            if user and user['Password'] == body.get('Password'):
                return build_response(200, {'message': 'OK', 'Role': user.get('Role', 'Member'), 'Name': user.get('Name'), 'UserID': user.get('UserID'), 'BorrowedBooks': user.get('BorrowedBooks', [])})
            return build_response(401, {'message': 'Fail'})

        else:
            return build_response(404, {'message': 'Route not found'})

    except Exception as e:
        print(f"Error: {str(e)}")
        return build_response(500, {'message': f'Server Error: {str(e)}'})
