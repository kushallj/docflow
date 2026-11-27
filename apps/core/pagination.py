from rest_framework.pagination import PageNumberPagination


class Pagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"   # e.g. the rule builder loads all rule sets at once
    max_page_size = 200
