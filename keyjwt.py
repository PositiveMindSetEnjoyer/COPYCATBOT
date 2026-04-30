import time
import jwt

ak = "AbAJBEdaHP4nTTRBrtTkeByfBAnYkgQH" # fill access key
sk = "anpdA3Cfyay4Ep4TryPfDK4dMDrpJJAe" # fill secret key

def encode_jwt_token(ak, sk):
    headers = {
        "alg": "HS256",
        "typ": "JWT"
    }
    payload = {
        "iss": ak,
        "exp": int(time.time()) + 60 * 60 * 24 * 30, # The valid time, in this example, represents the current time+1800s(30min)
        "nbf": int(time.time()) - 5 # The time when it starts to take effect, in this example, represents the current time -5s
    }
    token = jwt.encode(payload, sk, headers=headers)
    return token

authorization = encode_jwt_token(ak, sk)
print(authorization) # Printing the generated API_TOKEN