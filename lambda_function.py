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

        # 2. 書籍詳情
        elif path.startswith('/books/') and method == 'GET':
            isbn = path.split('/')[-1]
            resp = table_books.get_item(Key={'ISBN': isbn})
            return build_response(200, resp.get('Item')) if 'Item' in resp else build_response(404, {'message': 'Not found'})

        # 3. 新增書籍 (POST /admin/books)
        elif path == '/admin/books' and method == 'POST':
            if not body.get('ISBN') or not body.get('Title'):
                return build_response(400, {'message': '缺少必要欄位'})
            item = body
            # 清理與初始化
            item['TotalCopies'] = int(item.get('TotalCopies', 1))
            item['AvailableCopies'] = int(item.get('TotalCopies', 1))
            item['BorrowCount'] = 0
            item['Borrowers'] = [] 
            item['Status'] = 'Available'
            clean_item = {k: v for k, v in item.items() if v != ""}
            table_books.put_item(Item=clean_item)
            return build_response(201, {'message': '書籍新增成功'})

        # 4. 註冊 (POST /register)
        elif path == '/register' and method == 'POST':
            user_id = body.get('UserID')
            if not user_id: return build_response(400, {'message': '缺帳號'})
            
            check = table_users.get_item(Key={'UserID': user_id})
            if 'Item' in check: return build_response(400, {'message': '帳號已存在'})
            
            new_user = {
                'UserID': user_id,
                'Password': body.get('Password'),
                'Name': body.get('Name'),
                'Role': 'Member',
                'BorrowedBooks': []
            }
            table_users.put_item(Item=new_user)
            return build_response(201, {'message': '註冊成功'})

        # 5. 借閱書籍 (POST /books/{isbn}/borrow)
        elif path.startswith('/books/') and path.endswith('/borrow') and method == 'POST':
            isbn = path.split('/')[2]
            user_id = body.get('UserID')
            
            if not user_id: return build_response(400, {'message': '未登入'})

            book = table_books.get_item(Key={'ISBN': isbn}).get('Item')
            if not book: return build_response(404, {'message': 'Book not found'})
            if book['AvailableCopies'] <= 0: return build_response(400, {'message': '無庫存'})

            # 檢查是否重複借閱
            user = table_users.get_item(Key={'UserID': user_id}).get('Item')
            if not user: return build_response(404, {'message': 'User not found'})
            if isbn in user.get('BorrowedBooks', []):
                return build_response(400, {'message': '已借閱過此書'})

            # 執行借閱 (原子操作)
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

        # 6. 登入 (POST /login)
        elif path == '/login' and method == 'POST':
            input_id = body.get('UserID')
            input_pwd = body.get('Password')
            user = table_users.get_item(Key={'UserID': input_id}).get('Item')
            
            if user and user['Password'] == input_pwd:
                return build_response(200, {
                    'message': 'Login success',
                    'Role': user.get('Role', 'Member'),
                    'Name': user.get('Name'),
                    'UserID': user.get('UserID'),
                    'BorrowedBooks': user.get('BorrowedBooks', [])
                })
            return build_response(401, {'message': '帳號或密碼錯誤'})

        # 7. 刪除書籍 (DELETE /admin/books/{isbn}) - [重點功能]
        elif path.startswith('/admin/books/') and method == 'DELETE':
            isbn = path.split('/')[-1]
            # 這裡也可以加上檢查 UserID 是否為 Admin 的邏輯，但為了 demo 方便暫時略過
            table_books.delete_item(Key={'ISBN': isbn})
            return build_response(200, {'message': '書籍已刪除'})

        else:
            return build_response(404, {'message': 'Route not found'})

    except Exception as e:
        print(f"Error: {str(e)}")
        return build_response(500, {'message': f'Server Error: {str(e)}'})
