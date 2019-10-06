
def forum(request):
    '''
    Additional context applied to each request.
    Note: This function is critically important!
    The site will not load up without it.
    '''

    #print(request.session.get('res'), 'res', request.COOKIES)
    res = request.COOKIES.get('resolution', 'x')
    width, height = res.split('x')

    params = dict(user=request.user, width=width, height=height)

    return params
