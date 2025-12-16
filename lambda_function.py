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
        # ==================== 公開查詢 ====================
        
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

        # ==================== 管理員功能 ====================

        # 3. 新增書籍 (POST /admin/books)
        elif path == '/admin/books' and method == 'POST':
            if not body.get('ISBN') or not body.get('Title'):
                return build_response(400, {'message': '缺少 ISBN 或 Title'})
            item = body
            item['TotalCopies'] = int(item.get('TotalCopies', 1))
            item['AvailableCopies'] = int(item.get('TotalCopies', 1)) # 初始庫存=總數
            item['BorrowCount'] = 0
            item['Borrowers'] = []
            item['Status'] = 'Available'
            clean_item = {k: v for k, v in item.items() if v != ""}
            table_books.put_item(Item=clean_item)
            return build_response(201, {'message': '新增成功'})

        # 4. 修改書籍 (PUT /admin/books/{isbn}) [新功能]
        elif path.startswith('/admin/books/') and method == 'PUT':
            isbn = path.split('/')[-1]
            # 為了簡化，直接用 put_item 覆蓋 (但保留原有的 Borrowers 和 BorrowCount 以免資料遺失)
            old_book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if not old_book: return build_response(404, {'message': '書籍不存在'})
            
            item = body
            item['ISBN'] = isbn # 確保 Key 不變
            item['TotalCopies'] = int(item.get('TotalCopies', old_book.get('TotalCopies')))
            
            # 計算新的可用庫存：新總數 - (舊總數 - 舊可用數) = 新總數 - 已借出數
            borrowed_count = old_book['TotalCopies'] - old_book['AvailableCopies']
            item['AvailableCopies'] = item['TotalCopies'] - borrowed_count
            
            # 保留系統欄位
            item['BorrowCount'] = old_book.get('BorrowCount', 0)
            item['Borrowers'] = old_book.get('Borrowers', [])
            item['Status'] = 'Available' if item['AvailableCopies'] > 0 else 'OutOfStock'
            
            clean_item = {k: v for k, v in item.items() if v != ""}
            table_books.put_item(Item=clean_item)
            return build_response(200, {'message': '修改成功'})

        # 5. 刪除書籍 (DELETE /admin/books/{isbn})
        elif path.startswith('/admin/books/') and method == 'DELETE':
            isbn = path.split('/')[-1]
            table_books.delete_item(Key={'ISBN': isbn})
            return build_response(200, {'message': '刪除成功'})

        # ==================== 使用者操作 ====================

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

            # 原子更新
            table_books.update_item(
                Key={'ISBN': isbn},
                UpdateExpression="set AvailableCopies = AvailableCopies - :val, BorrowCount = BorrowCount + :val, Borrowers = list_append(if_not_exists(Borrowers, :empty), :uid)",
                ExpressionAttributeValues={':val': 1, ':uid': [user_id], ':empty': []}
            )
            table_users.update_item(
                Key={'UserID': user_id},
                UpdateExpression="set BorrowedBooks = list_append(if_not_exists(BorrowedBooks, :empty), :bid)",
                ExpressionAttributeValues={':bid': [isbn], ':empty': []}
            )
            return build_response(200, {'message': '借閱成功'})

        # 8. 還書 (POST /books/{isbn}/return) [新功能]
        elif path.startswith('/books/') and path.endswith('/return') and method == 'POST':
            isbn = path.split('/')[2]
            user_id = body.get('UserID')
            
            user = table_users.get_item(Key={'UserID': user_id}).get('Item')
            if not user or isbn not in user.get('BorrowedBooks', []):
                return build_response(400, {'message': '您未借閱此書'})

            # 1. 從使用者清單移除 (DynamoDB remove list item 比較複雜，這裡用較簡單的方式：讀出->修改->寫回)
            # 為了 Atomic 正確性，建議用 DELETE 操作，但 List 的 remove 需要 index。
            # 這裡簡化：更新 BorrowedBooks list
            new_books = [b for b in user['BorrowedBooks'] if b != isbn]
            table_users.update_item(
                Key={'UserID': user_id},
                UpdateExpression="set BorrowedBooks = :nb",
                ExpressionAttributeValues={':nb': new_books}
            )

            # 2. 更新書籍庫存 (庫存+1, 移除借閱者)
            # 同樣簡化：先讀後寫 Borrowers (實務上高併發需優化，但在 Lab 環境可接受)
            book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if book:
                new_borrowers = [u for u in book.get('Borrowers', []) if u != user_id]
                table_books.update_item(
                    Key={'ISBN': isbn},
                    UpdateExpression="set AvailableCopies = AvailableCopies + :val, Borrowers = :nbs",
                    ExpressionAttributeValues={':val': 1, ':nbs': new_borrowers}
                )

            return build_response(200, {'message': '還書成功'})

        # 9. 登入
        elif path == '/login' and method == 'POST':
            user = table_users.get_item(Key={'UserID': body.get('UserID')}).get('Item')
            if user and user['Password'] == body.get('Password'):
                return build_response(200, {
                    'message': 'OK', 'Role': user.get('Role', 'Member'),
                    'Name': user.get('Name'), 'UserID': user.get('UserID'),
                    'BorrowedBooks': user.get('BorrowedBooks', [])
                })
            return build_response(401, {'message': 'Fail'})

        else:
            return build_response(404, {'message': 'Route not found'})

    except Exception as e:
        print(f"Error: {str(e)}")
        return build_response(500, {'message': f'Server Error: {str(e)}'})
